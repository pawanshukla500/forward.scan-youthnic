"""Dispatch reconciliation: every AWB generated vs what was actually scanned, per sales channel.

An AWB generated today must be dispatched (scanned) today. For each AWB we know (last RETAIN_ORDERS_DAYS):

  scanned         - forward-scanned by the team (any day)
  pending         - not scanned (whatever OMS shows now, unless cancelled)    -> must go out
  overdue         - pending, and the AWB was generated before today           -> late
  left_unscanned  - INFO ONLY, part of pending: OMS already shows it shipped / in transit / no longer
                    Ready-to-ship, but nobody scanned it here
  cancelled       - not scanned, cancelled or returned after the AWB was made -> do not ship

Only a forward scan in this app takes an AWB out of pending (user, 8 Oct 2026): OMS moving an order to Shipped /
In Transit - Meesho does it as soon as the label is printed, Flipkart at manifest - must not make it disappear.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import MARKED_SHIPPED_FLAG, Channel, OmsOrder, Scan, SyncState
from ..timeutil import day_bounds_utc, dispatch_date_for, iso_utc, to_local, today_dispatch_date, utcnow
from . import cache, tracking

BUCKETS = ("pending", "overdue", "left_unscanned", "cancelled", "scanned", "generated")
_RANK = {"OPEN": 0, "NOT_PACKED": 1, "PARTIAL_CANCEL": 2, "UNKNOWN": 3, "MOVED": 4, "SHIPPED": 5, "RETURN": 6, "CANCELLED": 7,
         "REPLACED": 8}
# An AWB OMSGuru replaced with a new one (courier reassigned / label re-made, never scanned): not a shipment any
# more - not generated, not pending (sync._retire_replaced_awbs). The new AWB is the one counted.
NOT_A_SHIPMENT = ("REPLACED",)


# Statuses that are NOT pending (see AwbRec.bucket); used to push the "pending" filter into SQL.
_NOT_PENDING = NOT_PENDING = ("CANCELLED", "RETURN")
# Pending AWBs that OMS already shows as gone (counted as "left_unscanned" for information, still pending).
SHIPPED_IN_OMS = ("SHIPPED", "MOVED")


@dataclass
class AwbRec:
    awb: str
    channel_id: int | None
    status: str
    awb_at: datetime | None
    sla: datetime | None
    order_ids: list[int]
    scan_id: int | None
    # scanned by the one-time "already shipped in OMSGuru" mark, not by a packer (counted apart as marked_shipped)
    marked: bool = False
    scan_day: date | None = None  # the scan's dispatch day

    def bucket(self) -> str:
        if self.scan_id:
            return "scanned"
        if self.status in _NOT_PENDING:
            return "cancelled"
        return "pending"  # also when OMS already says Shipped / In Transit: only a scan takes it out

    @property
    def shipped_in_oms(self) -> bool:
        return self.bucket() == "pending" and self.status in SHIPPED_IN_OMS

    def awb_day(self) -> date | None:
        return dispatch_date_for(self.awb_at) if self.awb_at else None


def collect(db: Session, *, start: datetime | None = None, end: datetime | None = None,
            channel_id: int | None = None, pending_only: bool = False, scanned_on: date | None = None) -> list[AwbRec]:
    """One record per AWB (an AWB can carry two orders) with its scan, if any.

    awb_generated_at is always set for orders with an AWB (sync and startup fill it), so the range uses its index
    directly; only the scan id is read from scans (answered from the tracking_norm index). pending_only leaves out
    scanned / cancelled / shipped AWBs in SQL instead of loading them all; scanned_on keeps only the AWBs scanned on
    that dispatch day."""
    q = (
        select(OmsOrder.id, OmsOrder.tracking_norm, OmsOrder.channel_id, OmsOrder.status_group,
               OmsOrder.awb_generated_at, OmsOrder.sla_date, Scan.id, Scan.flags, Scan.dispatch_date)
        .outerjoin(Scan, Scan.tracking_norm == OmsOrder.tracking_norm)
        .where(OmsOrder.tracking_norm != "", OmsOrder.status_group.notin_(NOT_A_SHIPMENT))
    )
    if start is not None:
        q = q.where(OmsOrder.awb_generated_at >= start)
    if end is not None:
        q = q.where(OmsOrder.awb_generated_at < end)
    if channel_id:
        q = q.where(OmsOrder.channel_id == channel_id)
    if pending_only:
        q = q.where(OmsOrder.status_group.notin_(_NOT_PENDING), Scan.id.is_(None))
    if scanned_on is not None:
        q = q.where(Scan.dispatch_date == scanned_on)
    counted_from = tracking.start_utc()  # admin-set: AWBs made before this day are not counted
    if counted_from is not None:
        q = q.where(OmsOrder.awb_generated_at >= counted_from)
    recs: dict[str, AwbRec] = {}
    for oid, awb, cid, status, awb_at, sla, scan_id, scan_flags, scan_day in db.execute(q):
        r = recs.get(awb)
        if r is None:
            marked = bool(scan_flags) and MARKED_SHIPPED_FLAG in scan_flags.split(",")
            recs[awb] = AwbRec(awb, cid, status, awb_at, sla, [oid], scan_id, marked, scan_day)
            continue
        r.order_ids.append(oid)
        if _RANK.get(status, 3) < _RANK.get(r.status, 3):
            r.status = status
        r.channel_id = r.channel_id or cid
        if awb_at and (r.awb_at is None or awb_at < r.awb_at):
            r.awb_at = awb_at
    return list(recs.values())


def _simple(row: dict[str, int], is_today: bool) -> dict:
    """The one simple calculation the owner asked for (10 Oct 2026): SYNCED orders - SCANNED = PENDING.
    synced = every order to dispatch: the day's AWBs (cancelled ones excluded) and - for today - every AWB of an
    earlier day still not scanned or scanned today; scanned = those of them scanned (today: an earlier day's AWB
    scanned today counts here, so a scan always moves one order from pending to scanned); pending_all = the rest.
    No separate "overdue"."""
    scanned = row["scanned"] + (row["scanned_earlier"] if is_today else 0)
    pending_all = row["pending"] + (row["overdue"] if is_today else 0)
    synced = scanned + pending_all
    return {**row, "scanned": scanned, "pending_all": pending_all, "synced": synced,
            "pct": round(100 * scanned / synced) if synced > 0 else None}


def _empty() -> dict[str, int]:
    return {"generated": 0, "scanned": 0, "pending": 0, "overdue": 0, "left_unscanned": 0, "cancelled": 0,
            "marked_shipped": 0, "scanned_earlier": 0}


def summary(db: Session, day: date) -> dict[str, Any]:
    """Per-channel buckets for one AWB day, the overdue count and the strip of retained days - in one pass."""
    today = today_dispatch_date()
    first = today - timedelta(days=settings.retain_orders_days - 1)
    start, end = day_bounds_utc(day)
    today_start, _ = day_bounds_utc(today)
    since = min(start, day_bounds_utc(first)[0])
    chans = {c.id: c for c in db.scalars(select(Channel))}

    recs = collect(db, start=since)
    if day == today:  # still waiting from before the retained days (open orders are never pruned) or scanned today
        recs += collect(db, end=since, pending_only=True) + collect(db, end=since, scanned_on=today)

    per: dict[int | None, dict[str, int]] = {}
    by_day: dict[date, dict[str, int]] = {}
    for r in recs:
        b = r.bucket()
        d = r.awb_day()
        if d is not None and d >= first:
            row = by_day.setdefault(d, _empty())
            row["generated"] += 1
            row[b] += 1
            row["left_unscanned"] += r.shipped_in_oms
            row["marked_shipped"] += r.marked
        if r.awb_at is not None and start <= r.awb_at < end:
            row = per.setdefault(r.channel_id, _empty())
            row["generated"] += 1
            row[b] += 1
            row["left_unscanned"] += r.shipped_in_oms
            row["marked_shipped"] += r.marked
            if b == "pending" and day < today:
                row["overdue"] += 1
        elif day == today and r.awb_at is not None and r.awb_at < today_start:
            if b == "pending":  # an earlier day's AWB still not scanned: pending today
                row = per.setdefault(r.channel_id, _empty())
                row["overdue"] += 1
                row["left_unscanned"] += r.shipped_in_oms  # info: part of pending, like today's
            elif b == "scanned" and r.scan_day == today:  # an earlier day's AWB scanned today: today's work
                row = per.setdefault(r.channel_id, _empty())
                row["scanned_earlier"] += 1
                row["marked_shipped"] += r.marked

    channels = []
    for cid, row in per.items():
        c = chans.get(cid) if cid else None
        channels.append({
            "id": cid, "name": c.name if c else "Unmapped channel", "marketplace": c.marketplace if c else "",
            "color": c.color if c else "#898781", "sort_order": c.sort_order if c else 9999,
            **_simple(row, day == today),
        })
    channels.sort(key=lambda x: (x["sort_order"], x["name"]))
    totals = _empty()
    for row in per.values():
        for k in totals:
            totals[k] += row[k]
    totals = _simple(totals, day == today)
    strip = [{"date": (today - timedelta(days=i)).isoformat(), **by_day.get(today - timedelta(days=i), _empty())}
             for i in range(settings.retain_orders_days - 1, -1, -1)]
    return {
        "date": day.isoformat(), "is_today": day == today, "today": today.isoformat(),
        "retain_days": settings.retain_orders_days, "in_retention": day >= first,
        "counted_from": (tracking.start_date().isoformat() if tracking.start_date() else None),
        "trail": trail_check(db, day, chans),
        "totals": totals, "channels": channels, "days": strip,
    }


def trail_check(db: Session, day: date, chans: dict[int, Channel]) -> dict[str, Any] | None:
    """The hourly order-trail audit (oms/sync.py step_audit) for this AWB day: every AWB OMSGuru invoiced vs the
    AWBs here, per channel - the proof that "generated" (and so "pending") misses nothing. None = not checked."""
    row = db.get(SyncState, "trail_audit")
    try:
        days = (json.loads(row.value).get("days") or {}) if row and row.value else {}
    except ValueError:
        return None
    t = days.get(day.isoformat())
    if not t:
        return None
    out = []
    for cid, c in (t.get("channels") or {}).items():
        ch = chans.get(int(cid)) if cid not in ("0", "") else None
        out.append({"id": int(cid) if cid not in ("0", "") else None, "name": ch.name if ch else "Unmapped channel", **c})
    out.sort(key=lambda x: (-x["oms"], x["name"]))
    return ({k: t.get(k) for k in ("checked_at", "complete", "oms", "app", "added")}
            | {"extra": t.get("extra") or 0, "extra_awbs": t.get("extra_awbs") or [], "channels": out})


def summary_cached(db: Session, day: date) -> dict[str, Any]:
    """summary() for pages that reload it after every scan event (shared for a few seconds; read-only)."""
    if day != today_dispatch_date():
        return summary(db, day)
    return cache.cached(("summary", day), cache.ALL, 3.0, lambda: summary(db, day),
                        min_interval=settings.cache_min_interval)


def records_for(db: Session, day: date, bucket: str, channel_id: int | None) -> list[AwbRec]:
    today = today_dispatch_date()
    if bucket == "overdue":
        recs = collect(db, end=day_bounds_utc(today)[0], channel_id=channel_id, pending_only=True)
    elif bucket in ("pending", "left_unscanned") and day == today:
        # one pending list: today's AWBs not scanned AND every earlier one still not scanned (synced - scanned)
        recs = collect(db, start=day_bounds_utc(today)[0], channel_id=channel_id, pending_only=True)
        recs += collect(db, end=day_bounds_utc(today)[0], channel_id=channel_id, pending_only=True)
        if bucket == "left_unscanned":  # the pending ones OMS already shows as shipped
            recs = [r for r in recs if r.shipped_in_oms]
    elif bucket == "scanned" and day == today:
        # the same "scanned" the summary counts: today's AWBs scanned + earlier days' AWBs scanned today
        start, end = day_bounds_utc(today)
        recs = [r for r in collect(db, start=start, end=end, channel_id=channel_id) if r.bucket() == "scanned"]
        recs += collect(db, end=start, channel_id=channel_id, scanned_on=today)
    else:
        start, end = day_bounds_utc(day)
        recs = collect(db, start=start, end=end, channel_id=channel_id)
        if bucket == "left_unscanned":  # the pending ones OMS already shows as shipped
            recs = [r for r in recs if r.shipped_in_oms]
        elif bucket != "generated":
            recs = [r for r in recs if r.bucket() == bucket]
    far = datetime.max
    recs.sort(key=lambda r: (r.sla or far, r.awb_at or far, r.awb))
    return recs


def rows_payload(db: Session, recs: Iterable[AwbRec]) -> list[dict[str, Any]]:
    from .scanning import order_payload, scan_payload

    recs = list(recs)
    ids = [i for r in recs for i in r.order_ids]
    orders = {o.id: o for o in db.scalars(select(OmsOrder).where(OmsOrder.id.in_(ids or [0]))).unique()}
    scan_ids = [r.scan_id for r in recs if r.scan_id]
    scans = {s.id: s for s in db.scalars(select(Scan).where(Scan.id.in_(scan_ids or [0]))).unique()}
    today = today_dispatch_date()
    out = []
    for r in recs:
        o = orders.get(r.order_ids[0])
        awb_day = r.awb_day()
        s = scans.get(r.scan_id) if r.scan_id else None
        out.append({
            "awb": o.tracking_raw if o else r.awb,
            "bucket": r.bucket(),  # no separate "overdue": an earlier day's unscanned AWB is simply pending
            "status": r.status,
            "shipped_in_oms": r.shipped_in_oms,
            "awb_generated_at": iso_utc(r.awb_at),
            "awb_generated_local": to_local(r.awb_at).strftime("%d-%b %H:%M") if r.awb_at else "",
            "age_days": (today - awb_day).days if awb_day else None,
            "sla_breached": bool(r.sla and r.sla < utcnow()),
            "order": order_payload(o),
            "scan": scan_payload(s) if s else None,
        })
    return out
