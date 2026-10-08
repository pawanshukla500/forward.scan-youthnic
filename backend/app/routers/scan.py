from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import STAFF_ROLES, Channel, Manifest, OmsOrder, Scan, User
from ..oms.mapping import normalize_tracking
from ..oms.sync import get_engine
from ..security import current_user
from ..services import cache, tracking
from ..services.realtime import hub
from ..services.scanning import _event, find_orders, order_payload, process_scan, scan_payload
from ..timeutil import today_dispatch_date, utcnow

router = APIRouter(prefix="/api", tags=["scan"])


def pending_counts(db: Session) -> dict[int, int]:
    """Open (Packed / Ready-to-ship) shipments in OMS that nobody has scanned yet, per channel - counted from the
    admin-set start date."""
    q = (
        select(OmsOrder.channel_id, func.count(func.distinct(OmsOrder.tracking_norm)))
        .outerjoin(Scan, Scan.tracking_norm == OmsOrder.tracking_norm)
        .where(OmsOrder.status_group == "OPEN", OmsOrder.tracking_norm != "", Scan.id.is_(None))
        .group_by(OmsOrder.channel_id)
    )
    if (counted_from := tracking.start_utc()) is not None:
        q = q.where(OmsOrder.awb_generated_at >= counted_from)
    return {cid: n for cid, n in db.execute(q) if cid is not None}


def channel_payload(c: Channel) -> dict:
    return {"id": c.id, "name": c.name, "marketplace": c.marketplace, "company": c.company, "color": c.color,
            "oms_status": c.oms_status, "scan_enabled": c.scan_enabled, "sort_order": c.sort_order}


@router.get("/channels")
def list_channels(db: Session = Depends(get_db), user: User = Depends(current_user)):
    day = today_dispatch_date()
    chans = list(db.scalars(select(Channel).where(Channel.scan_enabled.is_(True)).order_by(Channel.sort_order, Channel.name)))
    counts = dict(
        db.execute(select(Scan.channel_id, func.count(Scan.id)).where(Scan.dispatch_date == day).group_by(Scan.channel_id)).all()
    )
    pend = pending_counts(db)
    from ..services.reconcile import summary_cached

    rec = {ch["id"]: ch for ch in summary_cached(db, day)["channels"]}
    blank = {"generated": 0, "scanned": 0, "pending": 0, "overdue": 0, "left_unscanned": 0, "cancelled": 0, "pct": None}
    return {
        "date": day.isoformat(),
        "channels": [
            {**channel_payload(c), "today": counts.get(c.id, 0), "pending": pend.get(c.id, 0),
             # AWBs generated today for this channel: how many are scanned / still to go / late from earlier days
             "awb_today": {k: rec.get(c.id, blank).get(k, v) for k, v in blank.items()}}
            for c in chans
        ],
    }


class ScanIn(BaseModel):
    channel_id: int
    tracking: str = Field(min_length=1, max_length=200)
    station: str = Field(default="", max_length=60)


@router.post("/scan")
def scan(body: ScanIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    channel = db.get(Channel, body.channel_id)
    if not channel or not channel.scan_enabled:
        raise HTTPException(400, "This sales channel is not enabled for scanning")
    engine = get_engine()
    res = process_scan(
        db, user=user, channel_id=body.channel_id, raw=body.tracking, station=body.station,
        on_unknown=engine.request_urgent if engine else None,
        live=engine.live_lookup if engine else None,
    )
    cache.invalidate(body.channel_id)  # this channel's counts and queue changed (or its rejected count did)
    if res.get("code") == "NOT_IN_OMS" and engine:
        b = engine.brief()
        if res.get("live") in ("busy", "timeout", "error"):
            res["message"] = ("OMSGuru did not answer in time - saved as UNVERIFIED, it verifies automatically "
                              "when the order syncs")
        elif b["invoices_failing"]:
            res["message"] = ("OMSGuru sync is failing right now, so new AWBs are not coming in - saved as UNVERIFIED, "
                              "it verifies automatically once the sync recovers")
        elif b["initial_load"]:
            res["message"] = (f"OMS order list is still loading ({b['cached_orders']:,} orders so far) - "
                              "saved as UNVERIFIED, it verifies automatically when the order arrives")
        else:
            res["message"] = ("Checked OMSGuru live: no order with this AWB yet - saved as UNVERIFIED. "
                              "Scan the ORDER ID barcode on the same label to fetch it now.")
    return res


@router.get("/sync/brief")
def sync_brief(user: User = Depends(current_user)):
    engine = get_engine()
    return engine.brief() if engine else {"mode": "off", "initial_load": False}


@router.get("/scans/recent")
def recent_scans(
    channel_id: int | None = None,
    limit: int = Query(30, ge=1, le=200),
    mine: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    day = today_dispatch_date()
    q = select(Scan).where(Scan.dispatch_date == day)
    if channel_id:
        q = q.where(Scan.channel_id == channel_id)
    if mine:
        q = q.where(Scan.user_id == user.id)
    rows = db.scalars(q.order_by(Scan.id.desc()).limit(limit)).unique()
    stats_q = select(Scan.result, func.count(Scan.id)).where(Scan.dispatch_date == day)
    if channel_id:
        stats_q = stats_q.where(Scan.channel_id == channel_id)
    stats = dict(db.execute(stats_q.group_by(Scan.result)).all())
    mine_count = db.scalar(select(func.count(Scan.id)).where(Scan.dispatch_date == day, Scan.user_id == user.id)) or 0
    return {"scans": [scan_payload(s) for s in rows], "stats": stats, "mine_today": mine_count}


@router.delete("/scans/{scan_id}")
def void_scan(scan_id: int, reason: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = db.get(Scan, scan_id)
    if not s:
        raise HTTPException(404, "Scan not found")
    own_recent = s.user_id == user.id and utcnow() - s.scanned_at < timedelta(minutes=10)
    if user.role not in STAFF_ROLES and not own_recent:
        raise HTTPException(403, "Only a supervisor can remove this scan (scanners can undo their own scans for 10 minutes)")
    m = db.get(Manifest, s.manifest_id) if s.manifest_id else None
    if m and m.status == "CLOSED" and user.role != "admin":
        raise HTTPException(400, "This shipment is in a closed manifest - only an admin can remove it")
    msg = f"Scan removed by {user.full_name or user.username}" + (f": {reason}" if reason else "")
    _event(db, user=user, station="", channel_id=s.channel_id, raw=s.tracking_raw, norm=s.tracking_norm,
           outcome="VOIDED", message=msg, scan_id=s.id)
    payload = {"id": s.id, "channel_id": s.channel_id, "tracking": s.tracking_raw}
    db.delete(s)
    db.commit()
    cache.invalidate(payload["channel_id"])
    hub.publish("scan_voided", payload)
    return {"ok": True}


# ---- scan-page context: everything the scan screen shows besides the scan itself ----------------


def _priority(sla, age_days: int | None) -> str:
    """Urgent = SLA passed or AWB from an earlier day; High = SLA within 3 hours; else Normal."""
    now = utcnow()
    if (age_days or 0) > 0 or (sla is not None and sla <= now):
        return "Urgent"
    if sla is not None and sla - now <= timedelta(hours=3):
        return "High"
    return "Normal"


@router.get("/scan-context")
def scan_context(channel_id: int, limit: int = Query(12, ge=1, le=50), db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    channel = db.get(Channel, channel_id)
    if not channel:
        raise HTTPException(404, "Sales channel not found")
    # Every station of the channel refetches this after each scan: compute it once and share it (at most once a
    # second per channel; "server_time" says how old it is, so a station can ask again for its own latest scan).
    return cache.cached(("scan-context", channel_id, limit), channel_id, 2.0, lambda: _scan_context(db, channel, limit),
                        min_interval=settings.cache_min_interval)


def _scan_context(db: Session, channel: Channel, limit: int) -> dict:
    from ..services import reconcile
    from ..timeutil import day_bounds_utc, iso_utc

    channel_id = channel.id
    day = today_dispatch_date()
    yday = day - timedelta(days=1)
    by_result = dict(db.execute(
        select(Scan.result, func.count(Scan.id)).where(Scan.dispatch_date == day, Scan.channel_id == channel_id).group_by(Scan.result)
    ).all())
    scanned = sum(by_result.values())
    alerts = db.scalar(select(func.count(Scan.id)).where(Scan.dispatch_date == day, Scan.channel_id == channel_id, Scan.alert != "")) or 0
    flagged_manual = db.scalar(select(func.count(Scan.id)).where(
        Scan.dispatch_date == day, Scan.channel_id == channel_id, Scan.flags.like("%FLAGGED%"))) or 0
    yesterday = db.scalar(select(func.count(Scan.id)).where(Scan.dispatch_date == yday, Scan.channel_id == channel_id)) or 0
    from ..models import ScanEvent
    rejected = db.scalar(select(func.count(ScanEvent.id)).where(
        ScanEvent.dispatch_date == day, ScanEvent.channel_id == channel_id,
        ScanEvent.outcome.in_(["DUPLICATE", "WRONG_CHANNEL", "BLOCKED"]))) or 0

    start, end = day_bounds_utc(day)
    today_recs = reconcile.collect(db, start=start, end=end, channel_id=channel_id)
    counts = {"generated": len(today_recs), "scanned": 0, "pending": 0, "left_unscanned": 0, "cancelled": 0}
    for r in today_recs:
        counts[r.bucket()] += 1
    earlier = reconcile.collect(db, end=start, channel_id=channel_id, pending_only=True)
    counts["overdue"] = len(earlier)
    base = counts["generated"] - counts["cancelled"]
    counts["pct"] = round(100 * counts["scanned"] / base) if base > 0 else None

    waiting = [r for r in today_recs if r.bucket() == "pending"] + earlier
    far = utcnow() + timedelta(days=3650)
    waiting.sort(key=lambda r: (r.sla or far, r.awb_at or far))
    rows = reconcile.rows_payload(db, waiting[:limit])
    queue = []
    for row in rows:
        o = row["order"] or {}
        items = o.get("items") or []
        sla_dt = None
        if o.get("sla_date"):
            from datetime import datetime as _dt
            sla_dt = _dt.fromisoformat(o["sla_date"].replace("Z", "+00:00")).replace(tzinfo=None)
        queue.append({
            "awb": row["awb"], "order_id": o.get("channel_order_id", ""), "courier": o.get("courier", ""),
            "skus": len({i.get("sku") for i in items}), "units": sum(int(i.get("qty") or 0) for i in items),
            "sla_date": o.get("sla_date"), "awb_generated_at": row["awb_generated_at"], "age_days": row["age_days"],
            "priority": _priority(sla_dt, row["age_days"]),
        })
    by_courier: dict[str, int] = {}
    if waiting:
        ids = [i for r in waiting for i in r.order_ids[:1]]
        for (courier,) in db.execute(select(OmsOrder.shipping_company).where(OmsOrder.id.in_(ids))):
            k = courier or "Unknown"
            by_courier[k] = by_courier.get(k, 0) + 1
    return {
        "date": day.isoformat(),
        "channel": channel_payload(channel),
        "stats": {"scanned": scanned, "ok": by_result.get("OK", 0),
                  "flagged": by_result.get("WARN", 0) + by_result.get("UNVERIFIED", 0) + alerts,
                  "flagged_manual": flagged_manual, "alerts": alerts, "rejected": rejected, "yesterday": yesterday},
        "awb": counts,
        "queue": queue,
        "queue_total": len(waiting),
        "pending_by_courier": dict(sorted(by_courier.items(), key=lambda kv: -kv[1])),
        "server_time": iso_utc(utcnow()),
    }


class FlagIn(BaseModel):
    reason: str = Field(min_length=2, max_length=60)
    note: str = Field(default="", max_length=200)


FLAG_REASONS = ("Missing item", "Wrong item", "Damaged packet", "Label problem", "Weight mismatch", "Other")


@router.post("/scans/{scan_id}/flag")
def flag_scan(scan_id: int, body: FlagIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Packer marks a scanned shipment for review (it stays scanned; supervisors see it as flagged)."""
    s = db.get(Scan, scan_id)
    if not s:
        raise HTTPException(404, "Scan not found")
    if user.role not in STAFF_ROLES and s.user_id != user.id:
        raise HTTPException(403, "You can only flag your own scans")
    flags = [f for f in (s.flags or "").split(",") if f and f != "FLAGGED"]
    s.flags = ",".join(flags + ["FLAGGED"])[:200]
    text = f"Flagged by {user.full_name or user.username}: {body.reason}" + (f" - {body.note}" if body.note else "")
    s.message = text[:300]
    if s.result == "OK":
        s.result = "WARN"
    _event(db, user=user, station=s.station, channel_id=s.channel_id, raw=s.tracking_raw, norm=s.tracking_norm,
           outcome="FLAGGED", message=text, scan_id=s.id)
    db.commit()
    db.refresh(s)
    cache.invalidate(s.channel_id)
    payload = scan_payload(s)
    hub.publish("scan_updated", payload)
    return {"ok": True, "scan": payload}


@router.get("/lookup")
def lookup(q: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    norm = normalize_tracking(q)
    if len(norm) < 3:
        raise HTTPException(400, "Enter at least 3 characters")
    scans = list(
        db.scalars(
            select(Scan).where(or_(Scan.tracking_norm == norm, Scan.tracking_norm.like(f"%{norm}%"))).order_by(Scan.id.desc()).limit(20)
        ).unique()
    )
    orders = find_orders(db, norm)
    if not orders and len(norm) >= 5:
        orders = list(
            db.scalars(
                select(OmsOrder).where(
                    or_(OmsOrder.tracking_norm.like(f"%{norm}%"), func.upper(OmsOrder.channel_order_id).like(f"%{norm}%"))
                ).limit(20)
            ).unique()
        )
    return {"scans": [scan_payload(s) for s in scans], "orders": [order_payload(o) for o in orders]}
