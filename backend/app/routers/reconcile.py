"""Pending & reconciliation (AWBs generated vs scanned) and long-term channel-wise scan history."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Channel, Scan, ScanEvent, User
from ..oms.sync import _get_state
from ..security import current_user
from ..services import reconcile, scanning
from ..services.exports import BUCKET_LABELS, channel_summary_xlsx, reconcile_xlsx
from ..timeutil import today_dispatch_date
from .reports import XLSX, _file, _parse_day

router = APIRouter(prefix="/api", tags=["reconciliation"])


@router.get("/reconciliation")
def reconciliation(day: str | None = Query(None, alias="date"), db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    check = _get_state("crosscheck")
    brief = {k: check.get(k) for k in ("ok", "retry", "checked_at", "oms", "local")} if check else None
    return {**reconcile.summary_cached(db, _parse_day(day)), "oms_check": brief}


def _bucket(b: str) -> str:
    if b not in reconcile.BUCKETS:
        raise HTTPException(400, f"bucket must be one of {', '.join(reconcile.BUCKETS)}")
    return b


@router.get("/reconciliation/list")
def reconciliation_list(
    day: str | None = Query(None, alias="date"), bucket: str = "pending", channel_id: int | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    recs = reconcile.records_for(db, _parse_day(day), _bucket(bucket), channel_id)
    chunk = recs[(page - 1) * page_size: page * page_size]
    return {"total": len(recs), "page": page, "page_size": page_size, "rows": reconcile.rows_payload(db, chunk)}


@router.get("/reconciliation/export.xlsx")
def reconciliation_export(
    day: str | None = Query(None, alias="date"), bucket: str = "pending", channel_id: int | None = None,
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    d = _parse_day(day)
    recs = reconcile.records_for(db, d, _bucket(bucket), channel_id)
    ch = db.get(Channel, channel_id) if channel_id else None
    title = (f"{BUCKET_LABELS[bucket]} - {ch.name if ch else 'All Sales Channels'} - "
             + ("AWBs generated before " + today_dispatch_date().strftime("%d-%m-%Y") if bucket == "overdue"
                else "AWBs generated on " + d.strftime("%d-%m-%Y")))
    content = reconcile_xlsx(reconcile.rows_payload(db, recs), title, reconcile.summary(db, d))
    return _file(content, f"awb_{bucket}_{d.isoformat()}{'_' + str(channel_id) if channel_id else ''}.xlsx", XLSX)


# ---- long-term, channel-wise scan history ------------------------------------------------------


def _channel_summary(db: Session, df: date, dt: date, group: str):
    if group not in ("day", "month"):
        raise HTTPException(400, "group must be day or month")
    key = (lambda d: d.strftime("%Y-%m")) if group == "month" else (lambda d: d.isoformat())
    cells: dict[tuple[str, int], dict[str, int]] = {}
    kind = scanning.kind_of()
    for d, cid, result, k, n in db.execute(
        select(Scan.dispatch_date, Scan.channel_id, Scan.result, kind, func.count(Scan.id))
        .where(Scan.dispatch_date >= df, Scan.dispatch_date <= dt)
        .group_by(Scan.dispatch_date, Scan.channel_id, Scan.result, kind)
    ):
        c = cells.setdefault((key(d), cid), {})
        if k == "marked":  # one-time "shipped in OMSGuru" marks: not scans by the team
            c["marked"] = c.get("marked", 0) + n
            continue
        c[result] = c.get(result, 0) + n
        if result in scanning.COUNTED_RESULTS:  # successful scans only; "Not found" = UNVERIFIED, apart
            c["scanned"] = c.get("scanned", 0) + n
    for d, cid, n in db.execute(
        select(Scan.dispatch_date, Scan.channel_id, func.count(Scan.id))
        .where(Scan.dispatch_date >= df, Scan.dispatch_date <= dt, Scan.alert != "")
        .group_by(Scan.dispatch_date, Scan.channel_id)
    ):
        c = cells.setdefault((key(d), cid), {})
        c["alerts"] = c.get("alerts", 0) + n
    for d, cid, outcome, n in db.execute(
        select(ScanEvent.dispatch_date, ScanEvent.channel_id, ScanEvent.outcome, func.count(ScanEvent.id))
        .where(ScanEvent.dispatch_date >= df, ScanEvent.dispatch_date <= dt,
               ScanEvent.outcome.in_(["DUPLICATE", "WRONG_CHANNEL", "BLOCKED", "NOT_FOUND"]))
        .group_by(ScanEvent.dispatch_date, ScanEvent.channel_id, ScanEvent.outcome)
    ):
        if cid is None:
            continue
        c = cells.setdefault((key(d), cid), {})
        k = "UNVERIFIED" if outcome == "NOT_FOUND" else outcome  # Not found attempts (not saved since 9 Oct)
        c[k] = c.get(k, 0) + n
    periods: list[str] = []
    seen: set[str] = set()
    d = df
    while d <= dt:
        k = key(d)
        if k not in seen:
            seen.add(k)
            periods.append(k)
        d += timedelta(days=1)
    used = {cid for (_, cid) in cells}
    chans = [{"id": c.id, "name": c.name, "marketplace": c.marketplace, "color": c.color}
             for c in db.scalars(select(Channel).order_by(Channel.sort_order, Channel.name)) if c.id in used]
    return periods, chans, cells


@router.get("/reports/channel-summary")
def channel_summary(date_from: str | None = None, date_to: str | None = None, group: str = "day",
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    dt = _parse_day(date_to)
    df = _parse_day(date_from) if date_from else dt - timedelta(days=29)
    if df > dt:
        raise HTTPException(400, "From date is after To date")
    if (dt - df).days > 3 * 366 + 31:
        raise HTTPException(400, "At most about 3 years at a time")
    periods, chans, cells = _channel_summary(db, df, dt, group)
    rows = [{"period": p, "total": sum(cells.get((p, c["id"]), {}).get("scanned", 0) for c in chans),
             "by_channel": {str(c["id"]): cells.get((p, c["id"]), {}) for c in chans if (p, c["id"]) in cells}}
            for p in periods]
    totals = {str(c["id"]): sum(cells.get((p, c["id"]), {}).get("scanned", 0) for p in periods) for c in chans}
    return {"date_from": df.isoformat(), "date_to": dt.isoformat(), "group": group, "channels": chans,
            "rows": rows, "totals": totals, "grand_total": sum(totals.values())}


@router.get("/reports/channel-summary.xlsx")
def channel_summary_export(date_from: str | None = None, date_to: str | None = None, group: str = "month",
                           db: Session = Depends(get_db), user: User = Depends(current_user)):
    dt = _parse_day(date_to)
    df = _parse_day(date_from) if date_from else dt - timedelta(days=364)
    if df > dt:
        raise HTTPException(400, "From date is after To date")
    if (dt - df).days > 3 * 366 + 31:  # as the page: an unbounded range ran for hours
        raise HTTPException(400, "At most about 3 years at a time")
    periods, chans, cells = _channel_summary(db, df, dt, group)
    title = f"Scanned shipments by sales channel ({'monthly' if group == 'month' else 'daily'}) - {df:%d-%m-%Y} to {dt:%d-%m-%Y}"
    return _file(channel_summary_xlsx(periods, chans, cells, title),
                 f"channel_summary_{group}_{df.isoformat()}_to_{dt.isoformat()}.xlsx", XLSX)
