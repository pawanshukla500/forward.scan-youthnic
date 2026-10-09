"""One-time clean-up (user, 8 Oct 2026): AWBs still pending here that OMSGuru already shows Shipped / In Transit
are marked as scanned, dated on the day OMSGuru shipped them (its shipment_date - the courier handover, on average
2-13 h after the team's scans of the same orders).

Why only once: the first days, before every packet went through this app, left ~2,400 AWBs pending that had long
left the warehouse. From then on the rule is leak-proof: only a real scan here takes an AWB out of Pending
(reconcile.py), so 1,000 AWBs with 800 scanned always shows 200 pending - whatever OMSGuru says.

A marked AWB is clearly not a packer's scan: user "OMSGuru (marked shipped)" (sign-in disabled), station
"OMSGuru shipped", flag MARKED_SHIPPED, an audit event MARKED_SHIPPED, and the Pending page counts them apart.
Only orders OMSGuru reports as SHIPPED with a shipment date are marked - Packed / Ready-to-ship ones and orders
whose status is unknown (MOVED) stay pending. If a packet that was marked is scanned for real later, that scan
replaces the mark (scanning.process_scan) and is OK, never "Duplicate - set aside".

Run: backend/mark_shipped_backlog.py (lists first; --yes marks).
"""
from __future__ import annotations

import secrets
from collections import Counter
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (BULK_SCAN_FLAG, BULK_SCAN_USERNAME, MARKED_SHIPPED_FLAG, SYSTEM_SHIPPED_USERNAME, Channel,
                      OmsOrder, Scan, User)
from ..security import hash_password
from ..timeutil import day_bounds_utc, dispatch_date_for, to_local, utcnow
from . import cache, reconcile
from .scanning import _event, _snapshot

STATION = "OMSGuru shipped"


def system_user(db: Session) -> User:
    """The account marked scans are recorded under. It can never sign in."""
    u = db.scalar(select(User).where(User.username == SYSTEM_SHIPPED_USERNAME))
    if u is None:
        u = User(username=SYSTEM_SHIPPED_USERNAME, full_name="OMSGuru (marked shipped)", email="",
                 password_hash=hash_password(secrets.token_urlsafe(32)), role="scanner", is_active=False)
        db.add(u)
        db.flush()
    return u


def candidates(db: Session, until: date | None = None) -> list[tuple[reconcile.AwbRec, OmsOrder]]:
    """Pending AWBs (counted, unscanned, not cancelled) that OMSGuru shows SHIPPED with a shipment date,
    shipped on or before [until]."""
    out = []
    for r in reconcile.collect(db, pending_only=True):
        if r.status != "SHIPPED" or not r.channel_id:
            continue
        orders = [o for o in (db.get(OmsOrder, i) for i in r.order_ids) if o is not None and o.shipment_date]
        if not orders:
            continue
        o = min(orders, key=lambda x: x.shipment_date)
        if until is not None and dispatch_date_for(o.shipment_date) > until:
            continue
        out.append((r, o))
    return out


def summarize(db: Session, found: list[tuple[reconcile.AwbRec, OmsOrder]]) -> Counter:
    """(shipped day, channel name) -> AWBs."""
    names = {c.id: c.name for c in db.scalars(select(Channel))}
    return Counter((dispatch_date_for(o.shipment_date), names.get(r.channel_id, "?")) for r, o in found)


def mark(db: Session, found: list[tuple[reconcile.AwbRec, OmsOrder]]) -> int:
    """Record each as scanned on its OMSGuru ship date. Skips any AWB a station scanned meanwhile."""
    user = system_user(db)
    done = 0
    for r, o in found:
        shipped_local = to_local(o.shipment_date)
        msg = (f"Marked scanned: OMSGuru shows it shipped on {shipped_local:%d-%b-%Y %H:%M} - "
               "it was not scanned in the app")
        try:
            with db.begin_nested():
                scan = Scan(
                    tracking_norm=r.awb, tracking_raw=(o.tracking_raw or r.awb)[:160],
                    dispatch_date=dispatch_date_for(o.shipment_date), scanned_at=o.shipment_date, user_id=user.id,
                    station=STATION, channel_id=r.channel_id, order_id=o.id, order_json=_snapshot(o),
                    result="OK", flags=MARKED_SHIPPED_FLAG, message=msg[:300],
                )
                db.add(scan)
                db.flush()
                _event(db, user=user, station=STATION, channel_id=r.channel_id, raw=scan.tracking_raw, norm=r.awb,
                       outcome="MARKED_SHIPPED", message=msg, scan_id=scan.id)
            done += 1
        except IntegrityError:
            continue  # scanned by a station a moment ago - nothing to mark
    db.commit()
    cache.clear()
    return done


# ---- admin bulk scan (owner-approved; backend/bulk_scan.py) -----------------------------------------------------

BULK_STATION = "Bulk scan (admin)"


def bulk_user(db: Session) -> User:
    """The account bulk scans are recorded under (reports show "Bulk scan (admin)"). It can never sign in."""
    u = db.scalar(select(User).where(User.username == BULK_SCAN_USERNAME))
    if u is None:
        u = User(username=BULK_SCAN_USERNAME, full_name="Bulk scan (admin)", email="",
                 password_hash=hash_password(secrets.token_urlsafe(32)), role="scanner", is_active=False)
        db.add(u)
        db.flush()
    return u


def bulk_candidates(db: Session, channel_id: int, day: date) -> list[reconcile.AwbRec]:
    """The AWBs of [day] (AWB generation day) still pending in this sales channel."""
    a, b = day_bounds_utc(day)
    return [r for r in reconcile.collect(db, start=a, end=b, channel_id=channel_id) if r.bucket() == "pending"]


def bulk_scan(db: Session, recs: list[reconcile.AwbRec], day: date, requested_by: str) -> int:
    """Record each pending AWB as a successful scan of [day], counted like a manual scan, under the bulk account.
    A packer scanning one of these packets later replaces it (OK); a station's scan a moment ago wins."""
    user = bulk_user(db)
    msg = (f"Bulk scan by admin ({requested_by}) - counted as scanned, not scanned at a station; a real scan of this "
           "packet replaces it")[:300]
    now, done = utcnow(), 0
    for r in recs:
        o = db.get(OmsOrder, r.order_ids[0])
        try:
            with db.begin_nested():
                s = Scan(tracking_norm=r.awb, tracking_raw=((o.tracking_raw if o else "") or r.awb)[:160], dispatch_date=day,
                         scanned_at=now, user_id=user.id, station=BULK_STATION, channel_id=r.channel_id,
                         order_id=o.id if o else None, order_json=_snapshot(o) if o else "", result="OK",
                         flags=BULK_SCAN_FLAG, message=msg)
                db.add(s)
                db.flush()
                _event(db, user=user, station=BULK_STATION, channel_id=r.channel_id, raw=s.tracking_raw, norm=r.awb,
                       outcome="BULK_SCAN", message=msg, scan_id=s.id)
            done += 1
        except IntegrityError:
            continue  # scanned at a station a moment ago
    db.commit()
    cache.clear()
    return done

