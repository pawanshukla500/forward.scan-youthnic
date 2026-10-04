from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Channel, OmsOrder, Scan, ScanEvent, User
from ..oms.mapping import normalize_tracking
from ..oms.sync import get_engine
from ..security import current_user, require_supervisor
from ..services import cache, tracking
from ..services.exports import pending_xlsx, scans_xlsx
from ..services.scanning import order_payload, scan_payload
from ..timeutil import iso_utc, to_local, today_dispatch_date, utcnow
from .scan import channel_payload, pending_counts

router = APIRouter(prefix="/api", tags=["reports"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file(content: bytes | str, filename: str, media: str) -> Response:
    return Response(
        content=content, media_type=media,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _parse_day(s: str | None) -> date:
    if not s:
        return today_dispatch_date()
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(400, "Date must be YYYY-MM-DD") from exc


@router.get("/dashboard")
def dashboard(day: str | None = Query(None, alias="date"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    d = _parse_day(day)
    if d != today_dispatch_date():
        return _dashboard(db, d)
    # every open dashboard reloads after scan events: compute today's once and share it
    return cache.cached(("dashboard", d), cache.ALL, 3.0, lambda: _dashboard(db, d),
                        min_interval=settings.cache_min_interval)


def _dashboard(db: Session, d: date) -> dict:
    is_today = d == today_dispatch_date()
    chans = list(db.scalars(select(Channel).order_by(Channel.sort_order, Channel.name)))

    per = {c.id: {"scanned": 0, "OK": 0, "WARN": 0, "UNVERIFIED": 0, "alerts": 0, "DUPLICATE": 0,
                  "WRONG_CHANNEL": 0, "BLOCKED": 0, "INVALID": 0} for c in chans}
    for cid, result, n in db.execute(
        select(Scan.channel_id, Scan.result, func.count(Scan.id)).where(Scan.dispatch_date == d).group_by(Scan.channel_id, Scan.result)
    ):
        if cid in per:
            per[cid][result] = n
            per[cid]["scanned"] += n
    for cid, n in db.execute(
        select(Scan.channel_id, func.count(Scan.id)).where(Scan.dispatch_date == d, Scan.alert != "").group_by(Scan.channel_id)
    ):
        if cid in per:
            per[cid]["alerts"] = n
    for cid, outcome, n in db.execute(
        select(ScanEvent.channel_id, ScanEvent.outcome, func.count(ScanEvent.id))
        .where(ScanEvent.dispatch_date == d, ScanEvent.outcome.in_(["DUPLICATE", "WRONG_CHANNEL", "BLOCKED", "INVALID"]))
        .group_by(ScanEvent.channel_id, ScanEvent.outcome)
    ):
        if cid in per:
            per[cid][outcome] = n

    pend = pending_counts(db) if is_today else {}
    channels = []
    for c in chans:
        p = per[c.id]
        if not (c.scan_enabled or p["scanned"] or pend.get(c.id)):
            continue
        channels.append({**channel_payload(c), **{k.lower(): v for k, v in p.items()}, "pending": pend.get(c.id, 0)})

    users = [
        {"user_id": uid, "name": name or uname, "scanned": n}
        for uid, name, uname, n in db.execute(
            select(Scan.user_id, User.full_name, User.username, func.count(Scan.id))
            .join(User, User.id == Scan.user_id)
            .where(Scan.dispatch_date == d)
            .group_by(Scan.user_id, User.full_name, User.username)
            .order_by(func.count(Scan.id).desc())
        )
    ]
    hourly = [0] * 24
    for (ts,) in db.execute(select(Scan.scanned_at).where(Scan.dispatch_date == d)):
        hourly[to_local(ts).hour] += 1

    totals = {k: sum(ch[k] for ch in channels) for k in
              ("scanned", "ok", "warn", "unverified", "alerts", "duplicate", "wrong_channel", "blocked", "invalid", "pending")}
    eng = get_engine()
    sync = None
    if eng:
        jobs = {j.name: j.as_dict() for j in eng.jobs.values()}
        sync = {"mode": "mock" if eng.client.__class__.__name__.startswith("Mock") else "live",
                "invoices": jobs.get("invoices"), "limiter": eng.client.state.snapshot()}
    # Same weekday last week; for today only up to this time of day, so a morning is not compared with a full day.
    wk = select(func.count(Scan.id)).where(Scan.dispatch_date == d - timedelta(days=7))
    if is_today:
        wk = wk.where(Scan.scanned_at <= utcnow() - timedelta(days=7))
    week_ago = db.scalar(wk) or 0
    metrics = {
        "success_rate": round(100 * totals["ok"] / totals["scanned"], 1) if totals["scanned"] else None,
        "flagged": totals["warn"] + totals["unverified"] + totals["alerts"],
        "rejected": totals["duplicate"] + totals["wrong_channel"] + totals["blocked"] + totals["invalid"],
        "avg_scan_seconds": avg_scan_seconds(db, d),
        "same_day_last_week": week_ago,
        "change_vs_last_week": round(100 * (totals["scanned"] - week_ago) / week_ago, 1) if week_ago else None,
    }
    return {"date": d.isoformat(), "is_today": is_today, "totals": totals, "channels": channels, "users": users,
            "hourly": hourly, "sync": sync, "metrics": metrics}


def avg_scan_seconds(db: Session, d: date, user_id: int | None = None) -> float | None:
    """Typical time between two scans of the same packer (gaps over 5 minutes are breaks, not scan time)."""
    q = select(Scan.user_id, Scan.scanned_at).where(Scan.dispatch_date == d).order_by(Scan.user_id, Scan.scanned_at)
    if user_id:
        q = q.where(Scan.user_id == user_id)
    gaps: list[float] = []
    prev: dict[int, object] = {}
    for uid, ts in db.execute(q):
        p = prev.get(uid)
        if p is not None:
            g = (ts - p).total_seconds()
            if 0 < g <= 300:
                gaps.append(g)
        prev[uid] = ts
    if not gaps:
        return None
    gaps.sort()
    return round(gaps[len(gaps) // 2], 1)  # median: robust to the odd long pause


def _scan_query(date_from: date, date_to: date, channel_id: int | None, user_id: int | None, result: str | None,
                q: str | None, alerts_only: bool, courier: str | None = None):
    stmt = select(Scan).where(Scan.dispatch_date >= date_from, Scan.dispatch_date <= date_to)
    if channel_id:
        stmt = stmt.where(Scan.channel_id == channel_id)
    if user_id:
        stmt = stmt.where(Scan.user_id == user_id)
    if result == "FLAGGED":  # anything that needs a look: check / unverified / flagged by a packer / alert
        stmt = stmt.where(or_(Scan.result.in_(["WARN", "UNVERIFIED"]), Scan.alert != "", Scan.flags.like("%FLAGGED%")))
    elif result:
        stmt = stmt.where(Scan.result == result)
    if courier:
        stmt = stmt.where(Scan.order_json.like(f'%"courier": {json.dumps(courier)}%'))
    if alerts_only:
        stmt = stmt.where(Scan.alert != "")
    if q:
        norm = normalize_tracking(q)
        # Order id / invoice live in the copy saved with each scan (the order itself may be pruned).
        stmt = stmt.where(or_(Scan.tracking_norm.like(f"%{norm}%"), func.upper(Scan.order_json).like(f"%{norm}%")))
    return stmt


@router.get("/scans")
def list_scans(
    date_from: str | None = None, date_to: str | None = None, channel_id: int | None = None,
    user_id: int | None = None, result: str | None = None, q: str | None = None, alerts_only: bool = False,
    courier: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    df, dt = _parse_day(date_from), _parse_day(date_to or date_from)
    stmt = _scan_query(df, dt, channel_id, user_id, result, q, alerts_only, courier)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(stmt.order_by(Scan.id.desc()).offset((page - 1) * page_size).limit(page_size)).unique()
    return {"total": total, "page": page, "page_size": page_size, "scans": [scan_payload(s) for s in rows]}


@router.get("/scans/export.xlsx")
def export_scans(
    date_from: str | None = None, date_to: str | None = None, channel_id: int | None = None,
    user_id: int | None = None, result: str | None = None, q: str | None = None, alerts_only: bool = False,
    courier: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user),
):
    df, dt = _parse_day(date_from), _parse_day(date_to or date_from)
    if (dt - df).days > 62:
        raise HTTPException(400, "Export at most 62 days at a time")
    stmt = _scan_query(df, dt, channel_id, user_id, result, q, alerts_only, courier)
    scans = list(db.scalars(stmt.order_by(Scan.channel_id, Scan.scanned_at)).unique())
    ch = db.get(Channel, channel_id) if channel_id else None
    span = df.strftime("%d-%m-%Y") + ("" if df == dt else " to " + dt.strftime("%d-%m-%Y"))
    title = f"Forward Dispatch Report - {ch.name if ch else 'All Sales Channels'} - {span}"
    by_channel: dict[str, int] = {}
    for s in scans:
        by_channel[s.channel.name if s.channel else "?"] = by_channel.get(s.channel.name if s.channel else "?", 0) + 1
    summary = [("Total shipments", len(scans))] + sorted(by_channel.items())
    name = f"dispatch_{df.isoformat()}{'' if df == dt else '_to_' + dt.isoformat()}{'_' + str(channel_id) if channel_id else ''}.xlsx"
    return _file(scans_xlsx(scans, title, summary), name, XLSX)


@router.get("/scans/export.csv")
def export_scans_csv(
    date_from: str | None = None, date_to: str | None = None, channel_id: int | None = None,
    user_id: int | None = None, result: str | None = None, q: str | None = None, alerts_only: bool = False,
    courier: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user),
):
    import csv
    import io

    from ..services.exports import SCAN_COLUMNS, scan_row

    df, dt = _parse_day(date_from), _parse_day(date_to or date_from)
    if (dt - df).days > 62:
        raise HTTPException(400, "Export at most 62 days at a time")
    scans = db.scalars(_scan_query(df, dt, channel_id, user_id, result, q, alerts_only, courier)
                       .order_by(Scan.channel_id, Scan.scanned_at)).unique()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([c for c, _ in SCAN_COLUMNS])
    for i, s in enumerate(scans, start=1):
        w.writerow(scan_row(i, s))
    name = f"dispatch_{df.isoformat()}{'' if df == dt else '_to_' + dt.isoformat()}.csv"
    return _file("\ufeff" + out.getvalue(), name, "text/csv; charset=utf-8")


@router.get("/reports/filters")
def report_filters(date_from: str | None = None, date_to: str | None = None, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """Couriers and operators that actually appear in the selected dates."""
    df, dt = _parse_day(date_from), _parse_day(date_to or date_from)
    couriers: set[str] = set()
    for (oj,) in db.execute(select(Scan.order_json).where(Scan.dispatch_date >= df, Scan.dispatch_date <= dt, Scan.order_json != "")):
        try:
            c = json.loads(oj).get("courier")
        except ValueError:
            c = None
        if c:
            couriers.add(c)
    ops = db.execute(
        select(User.id, User.full_name, User.username).join(Scan, Scan.user_id == User.id)
        .where(Scan.dispatch_date >= df, Scan.dispatch_date <= dt).group_by(User.id, User.full_name, User.username)
    ).all()
    return {"couriers": sorted(couriers), "operators": [{"id": i, "name": n or u} for i, n, u in ops]}


@router.get("/reports/sku-summary")
def sku_summary(date_from: str | None = None, date_to: str | None = None, channel_id: int | None = None,
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Units scanned per SKU (from scans) and units still waiting (Ready-to-ship, not scanned)."""
    from ..services import reconcile

    df, dt = _parse_day(date_from), _parse_day(date_to or date_from)
    chans = {c.id: c.name for c in db.scalars(select(Channel))}
    agg: dict[str, dict] = {}
    q = select(Scan.order_json, Scan.channel_id).where(Scan.dispatch_date >= df, Scan.dispatch_date <= dt, Scan.order_json != "")
    if channel_id:
        q = q.where(Scan.channel_id == channel_id)
    for oj, cid in db.execute(q):
        try:
            items = json.loads(oj).get("items") or []
        except ValueError:
            continue
        for it in items:
            a = agg.setdefault(it.get("sku") or "?", {"sku": it.get("sku") or "?", "scanned": 0, "pending": 0, "by_channel": {}})
            n = int(it.get("qty") or 0)
            a["scanned"] += n
            a["by_channel"][cid] = a["by_channel"].get(cid, 0) + n
    waiting = reconcile.collect(db, channel_id=channel_id, pending_only=True)
    ids = [i for r in waiting for i in r.order_ids]
    for o in db.scalars(select(OmsOrder).where(OmsOrder.id.in_(ids or [0]))):
        try:
            items = json.loads(o.items_json or "[]")
        except ValueError:
            continue
        for it in items:
            a = agg.setdefault(it.get("sku") or "?", {"sku": it.get("sku") or "?", "scanned": 0, "pending": 0, "by_channel": {}})
            a["pending"] += int(it.get("qty") or 0)
    rows = []
    for a in agg.values():
        top = max(a["by_channel"].items(), key=lambda kv: kv[1]) if a["by_channel"] else None
        share = round(100 * top[1] / a["scanned"]) if top and a["scanned"] else None
        rows.append({"sku": a["sku"], "scanned": a["scanned"], "pending": a["pending"],
                     "top_channel": chans.get(top[0], "") if top else "", "top_share": share})
    rows.sort(key=lambda r: (-r["scanned"], -r["pending"], r["sku"]))
    return {"date_from": df.isoformat(), "date_to": dt.isoformat(), "rows": rows}


@router.get("/reports/operators")
def operators(date_from: str | None = None, date_to: str | None = None, channel_id: int | None = None,
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    df, dt = _parse_day(date_from), _parse_day(date_to or date_from)
    q = select(Scan.user_id, Scan.result, Scan.alert, Scan.flags, Scan.station, Scan.scanned_at).where(
        Scan.dispatch_date >= df, Scan.dispatch_date <= dt)
    if channel_id:
        q = q.where(Scan.channel_id == channel_id)
    people: dict[int, dict] = {}
    for uid, result, alert, flags, station, ts in db.execute(q):
        p = people.setdefault(uid, {"scans": 0, "ok": 0, "flagged": 0, "stations": set(), "first": ts, "last": ts})
        p["scans"] += 1
        p["ok"] += result == "OK" and not alert
        p["flagged"] += result != "OK" or bool(alert) or "FLAGGED" in (flags or "")
        if station:
            p["stations"].add(station)
        p["first"], p["last"] = min(p["first"], ts), max(p["last"], ts)
    ev = dict(db.execute(
        select(ScanEvent.user_id, func.count(ScanEvent.id)).where(
            ScanEvent.dispatch_date >= df, ScanEvent.dispatch_date <= dt,
            ScanEvent.outcome.in_(["DUPLICATE", "WRONG_CHANNEL", "BLOCKED", "INVALID"]))
        .group_by(ScanEvent.user_id)).all())
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(list(people) or [0])))}
    rows = []
    for uid, p in people.items():
        u = users.get(uid)
        rows.append({
            "user_id": uid, "name": (u.full_name or u.username) if u else "?", "scans": p["scans"],
            "success_rate": round(100 * p["ok"] / p["scans"], 1) if p["scans"] else None, "flagged": p["flagged"],
            "rejected": ev.get(uid, 0), "avg_scan_seconds": avg_scan_seconds(db, df, uid) if df == dt else None,
            "stations": sorted(p["stations"]), "first": iso_utc(p["first"]), "last": iso_utc(p["last"]),
        })
    rows.sort(key=lambda r: -r["scans"])
    return {"rows": rows}


@router.get("/marketplaces")
def marketplaces(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Sales channels with their sync and dispatch numbers (Marketplaces page)."""
    from ..services.reconcile import summary_cached

    day = today_dispatch_date()
    rec = {c["id"]: c for c in summary_cached(db, day)["channels"]}
    awb_q = select(OmsOrder.channel_id, func.count(func.distinct(OmsOrder.tracking_norm))).where(OmsOrder.tracking_norm != "")
    if (counted_from := tracking.start_utc()) is not None:
        awb_q = awb_q.where(OmsOrder.awb_generated_at >= counted_from)
    awb7 = dict(db.execute(awb_q.group_by(OmsOrder.channel_id)).all())
    last = dict(db.execute(select(OmsOrder.channel_id, func.max(OmsOrder.synced_at)).group_by(OmsOrder.channel_id)).all())
    scans_today = dict(db.execute(select(Scan.channel_id, func.count(Scan.id)).where(Scan.dispatch_date == day)
                                  .group_by(Scan.channel_id)).all())
    eng = get_engine()
    sync_ok = None
    if eng:
        inv = eng.jobs.get("invoices")
        sync_ok = bool(inv and inv.last_ok is not False)
    out = []
    for c in db.scalars(select(Channel).order_by(Channel.sort_order, Channel.name)):
        r = rec.get(c.id, {})
        out.append({
            **channel_payload(c), "awb_7d": awb7.get(c.id, 0), "last_synced_at": iso_utc(last.get(c.id)),
            "scans_today": scans_today.get(c.id, 0),
            "today": {k: r.get(k, 0) for k in ("generated", "scanned", "pending", "overdue", "left_unscanned", "cancelled")},
            "pct": r.get("pct"),
        })
    return {"channels": out, "sync_ok": sync_ok, "retain_days": settings.retain_orders_days}


@router.get("/events")
def list_events(
    day: str | None = Query(None, alias="date"), outcome: str | None = None, channel_id: int | None = None,
    limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db), user: User = Depends(current_user),
):
    d = _parse_day(day)
    stmt = select(ScanEvent).where(ScanEvent.dispatch_date == d)
    if outcome:
        stmt = stmt.where(ScanEvent.outcome.in_(outcome.split(",")))
    if channel_id:
        stmt = stmt.where(ScanEvent.channel_id == channel_id)
    chans = {c.id: c.name for c in db.scalars(select(Channel))}
    return {
        "events": [
            {"id": e.id, "created_at": iso_utc(e.created_at), "local": to_local(e.created_at).strftime("%d-%b %H:%M:%S"),
             "user": (e.user.full_name or e.user.username) if e.user else "", "station": e.station,
             "channel": chans.get(e.channel_id, ""), "channel_id": e.channel_id, "tracking": e.tracking_raw,
             "outcome": e.outcome, "message": e.message, "scan_id": e.scan_id}
            for e in db.scalars(stmt.order_by(ScanEvent.id.desc()).limit(limit)).unique()
        ]
    }


def _pending_query(channel_id: int | None):
    stmt = (
        select(OmsOrder)
        .outerjoin(Scan, Scan.tracking_norm == OmsOrder.tracking_norm)
        .where(OmsOrder.status_group == "OPEN", OmsOrder.tracking_norm != "", Scan.id.is_(None))
    )
    if channel_id:
        stmt = stmt.where(OmsOrder.channel_id == channel_id)
    if (counted_from := tracking.start_utc()) is not None:
        stmt = stmt.where(OmsOrder.awb_generated_at >= counted_from)
    return stmt


@router.get("/pending")
def pending(channel_id: int | None = None, page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = _pending_query(channel_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(OmsOrder.sla_date.is_(None), OmsOrder.sla_date, OmsOrder.order_date).offset((page - 1) * page_size).limit(page_size)
    ).unique()
    return {"total": total, "counts": pending_counts(db), "orders": [order_payload(o) for o in rows]}


@router.get("/pending/export.xlsx")
def export_pending(channel_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_supervisor)):
    rows = list(db.scalars(_pending_query(channel_id).order_by(OmsOrder.channel_id, OmsOrder.sla_date)).unique())
    ch = db.get(Channel, channel_id) if channel_id else None
    title = f"Pending (not scanned) Ready-to-ship shipments - {ch.name if ch else 'All Sales Channels'}"
    return _file(pending_xlsx(rows, title), f"pending_{today_dispatch_date().isoformat()}.xlsx", XLSX)


@router.get("/history")
def history(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Date-wise totals per channel for the last N business days."""
    end = today_dispatch_date()
    start = end - timedelta(days=days - 1)
    rows = db.execute(
        select(Scan.dispatch_date, Scan.channel_id, func.count(Scan.id))
        .where(Scan.dispatch_date >= start, Scan.dispatch_date <= end)
        .group_by(Scan.dispatch_date, Scan.channel_id)
    ).all()
    out: dict[str, dict] = {}
    for d, cid, n in rows:
        day = out.setdefault(d.isoformat(), {"date": d.isoformat(), "total": 0, "by_channel": {}})
        day["by_channel"][str(cid)] = n
        day["total"] += n
    series = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        series.append(out.get(d, {"date": d, "total": 0, "by_channel": {}}))
    return {"days": series}
