"""Core forward-scan logic: lookup, duplicate guard, marketplace check, status check."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import String, and_, case, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import MARKED_SHIPPED_FLAG, Channel, Manifest, OmsOrder, Scan, ScanEvent, SkuPhoto, User
from ..oms.mapping import normalize_sku_key, normalize_tracking
from ..timeutil import dispatch_date_for, iso_utc, to_local, today_dispatch_date, utcnow
from . import awb_shapes, cache
from .realtime import hub

MIN_TRACKING_LEN = 6
MAX_TRACKING_LEN = 40
log = logging.getLogger("scan")

# Severity -> what the station plays/shows.
#   success : green, short beep        (accepted)
#   warning : amber, double beep       (accepted but needs a look)
#   error   : red, long buzz           (rejected, not counted)


# What counts as a scan (user, 9 Oct 2026: "count the successful scans only"): verified against OMSGuru - OK, or
# WARN (saved, check the packet). A "Not found" (UNVERIFIED) scan is flagged and counted on its own until the order
# syncs and it turns OK by itself; the one-time "shipped in OMSGuru" marks keep AWBs out of Pending but are not scans
# by the team.
COUNTED_RESULTS = ("OK", "WARN")


def is_mark():
    """SQL: the scan row is a one-time "shipped in OMSGuru" mark, not a scan."""
    return func.coalesce(Scan.flags, "").like(f"%{MARKED_SHIPPED_FLAG}%")


def counted():
    """SQL: the scan counts as scanned (successful, by the team)."""
    return and_(Scan.result.in_(COUNTED_RESULTS), ~is_mark())


def kind_of():
    """SQL: 'scanned' | 'not_found' | 'marked' per scan row, for GROUP BY."""
    return case((is_mark(), "marked"), (Scan.result == "UNVERIFIED", "not_found"), else_="scanned")


@dataclass
class Verdict:
    result: str          # OK | WARN | UNVERIFIED | BLOCK
    flags: list[str] = field(default_factory=list)
    message: str = ""
    outcome: str = ""    # ScanEvent outcome when blocked: WRONG_CHANNEL | BLOCKED


def evaluate(orders: list[OmsOrder], selected_channel_id: int, channels: dict[int, Channel]) -> Verdict:
    """Decide whether a shipment may go into the selected sales channel's dispatch."""
    if not orders:
        return Verdict("UNVERIFIED", ["NOT_IN_OMS"], "Not found in OMS yet - accepted as UNVERIFIED, will auto-verify on next sync")

    order_channels = {o.channel_id for o in orders if o.channel_id}
    if order_channels and selected_channel_id not in order_channels:
        other = channels.get(next(iter(order_channels)))
        name = other.name if other else orders[0].channel_label
        return Verdict("BLOCK", ["WRONG_CHANNEL"], f"WRONG MARKETPLACE - this shipment belongs to {name}", "WRONG_CHANNEL")

    groups = {o.status_group for o in orders}
    if groups <= {"REPLACED"}:
        newer = (orders[0].status_text or "").removeprefix("AWB replaced by").strip()
        return Verdict("BLOCK", ["REPLACED"], "OLD LABEL - OMSGuru replaced this AWB"
                       + (f" with {newer}" if newer else "") + ": print the new label and scan that", "BLOCKED")
    groups -= {"REPLACED"}
    if groups <= {"CANCELLED"}:
        return Verdict("BLOCK", ["CANCELLED"], "ORDER CANCELLED in OMS - do NOT dispatch, keep aside", "BLOCKED")
    if "RETURN" in groups:
        return Verdict("BLOCK", ["RETURN"], "Order is in RETURN status in OMS - do NOT dispatch", "BLOCKED")

    flags: list[str] = []
    notes: list[str] = []
    if not order_channels:
        flags.append("CHANNEL_UNMAPPED")
        notes.append(f"OMS channel '{orders[0].channel_label}' is not mapped to a sales channel")
    if "PARTIAL_CANCEL" in groups or "CANCELLED" in groups:
        flags.append("PARTIAL_CANCEL")
        notes.append("Some items were cancelled - check the packet contents")
    if "NOT_PACKED" in groups:
        flags.append("NOT_RTS")
        notes.append("OMS still shows this order as New/Pending (not packed)")
    # Shipped / In Transit in OMS is normal, not a check: marketplaces such as Meesho (Valmo) mark the
    # order In Transit as soon as the label is made, and every scanned packet reaches it after pickup -
    # flagging it made good scans amber and counted them as "needs review" (user, 5 Oct 2026).
    # "MOVED" (left Packed / Ready-to-ship, status not fetched yet) is the same: 97 % turn out Shipped / In Transit
    # (exit check, 8 Oct 2026). It scans OK and leaves Pending; a cancellation found later still raises the
    # "AFTER SCAN" alert on the scan (reverify_scans), so a cancelled packet is not missed.
    if flags:
        return Verdict("WARN", flags, "; ".join(notes))
    return Verdict("OK", [], "Verified")


def find_orders(db: Session, norm: str) -> list[OmsOrder]:
    """Local lookup: by AWB first; else the barcode is an order / sub-order / invoice number."""
    if not norm:
        return []
    rows = list(db.scalars(select(OmsOrder).where(OmsOrder.tracking_norm == norm)).unique())
    if rows:
        return rows
    sub_list = literal(",", String) + func.upper(OmsOrder.sub_order_ids) + literal(",", String)
    rows = list(
        db.scalars(
            select(OmsOrder)
            .where(
                or_(
                    func.upper(OmsOrder.channel_order_id) == norm,
                    func.upper(OmsOrder.invoice_id) == norm,
                    sub_list.like(f"%,{norm},%"),  # exact sub-order id inside the comma list
                )
            )
            .limit(20)
        ).unique()
    )
    return pick_shipment(rows)


OPEN_GROUPS = ("OPEN", "NOT_PACKED", "PARTIAL_CANCEL")


def pick_shipment(rows: list[OmsOrder]) -> list[OmsOrder]:
    """One order id can have several shipments (e.g. a re-shipment after a return, each with its own AWB).
    For an order-id scan keep the single shipment that can still be dispatched; if that is not
    clear-cut, return them all and the scan asks for the AWB instead."""
    by_awb: dict[str, list[OmsOrder]] = {}
    for r in rows:
        by_awb.setdefault(r.tracking_norm or f"#{r.id}", []).append(r)
    if len(by_awb) <= 1:
        return rows
    dispatchable = [g for g in by_awb.values() if any(o.status_group in OPEN_GROUPS for o in g)]
    return dispatchable[0] if len(dispatchable) == 1 else rows


def order_payload(o: OmsOrder | None) -> dict[str, Any] | None:
    if not o:
        return None
    try:
        items = json.loads(o.items_json or "[]")
    except ValueError:
        items = []

    missing_skus = [normalize_sku_key(i.get("sku")) for i in items if isinstance(i, dict) and not i.get("image_url") and i.get("sku")]
    if missing_skus:
        from ..db import SessionLocal
        try:
            with SessionLocal() as db:
                p_rows = db.scalars(select(SkuPhoto).where(SkuPhoto.sku.in_(missing_skus))).all()
                p_map = {p.sku: (p.image_url, p.title) for p in p_rows}
                for it in items:
                    if isinstance(it, dict):
                        sk = normalize_sku_key(it.get("sku"))
                        if not it.get("image_url") and sk in p_map:
                            it["image_url"] = p_map[sk][0]
                            if not it.get("title") and p_map[sk][1]:
                                it["title"] = p_map[sk][1]
        except Exception:
            pass

    return {
        "id": o.id,
        "channel_label": o.channel_label,
        "channel_id": o.channel_id,
        "company": o.company,
        "warehouse": o.warehouse,
        "channel_order_id": o.channel_order_id,
        "sub_order_ids": [s for s in (o.sub_order_ids or "").split(",") if s],
        "invoice_id": o.invoice_id,
        "invoice_date": iso_utc(o.invoice_date),
        "order_date": iso_utc(o.order_date),
        "sla_date": iso_utc(o.sla_date),
        "tracking": o.tracking_raw,
        "courier": o.shipping_company,
        "order_type": o.order_type,
        "buyer_name": o.buyer_name,
        "buyer_city": o.buyer_city,
        "buyer_state": o.buyer_state,
        "buyer_pincode": o.buyer_pincode,
        "item_count": o.item_count,
        "total_qty": o.total_qty,
        "total_amount": o.total_amount,
        "currency": o.currency,
        "items": items,
        "status_text": o.status_text,
        "status_group": o.status_group,
        "synced_at": iso_utc(o.synced_at),
        "awb_generated_at": iso_utc(o.awb_generated_at or o.invoice_date),
        # "today" = AWB made today, must go out today; "overdue" = AWB made on an earlier day
        "dispatch_due": _due(o.awb_generated_at or o.invoice_date),
    }


def _due(awb_at) -> dict[str, Any] | None:
    if not awb_at:
        return None
    age = (today_dispatch_date() - dispatch_date_for(awb_at)).days
    return {"state": "today" if age <= 0 else "overdue", "age_days": max(0, age)}


def scan_order(s: Scan) -> dict[str, Any] | None:
    """Order details for a scan: live working-set row if still there, else the copy saved with the scan."""
    if s.order is not None:
        return order_payload(s.order)
    if s.order_json:
        try:
            return json.loads(s.order_json)
        except ValueError:
            return None
    return None


def _snapshot(o: OmsOrder | None) -> str:
    return json.dumps(order_payload(o), default=str, ensure_ascii=False) if o else ""


def scan_payload(s: Scan) -> dict[str, Any]:
    local = to_local(s.scanned_at)
    return {
        "id": s.id,
        "tracking": s.tracking_raw,
        "tracking_norm": s.tracking_norm,
        "dispatch_date": s.dispatch_date.isoformat(),
        "scanned_at": iso_utc(s.scanned_at),
        "scanned_at_local": local.strftime("%d-%b %H:%M:%S"),
        "user": (s.user.full_name or s.user.username) if s.user else "",
        "user_id": s.user_id,
        "station": s.station,
        "channel_id": s.channel_id,
        "channel_name": s.channel.name if s.channel else "",
        "marketplace": s.channel.marketplace if s.channel else "",
        "result": s.result,
        "flags": [f for f in (s.flags or "").split(",") if f],
        "message": s.message,
        "alert": s.alert,
        "manifest_id": s.manifest_id,
        "manifest": {
            "status": s.manifest.status,
            "closed_at": iso_utc(s.manifest.closed_at),
            "number": f"M-{s.manifest.dispatch_date.strftime('%Y%m%d')}-{s.manifest.channel_id}-{s.manifest.seq}",
        } if s.manifest else None,
        "oms_update_status": s.oms_update_status,
        "order": scan_order(s),
    }


def _open_manifest(db: Session, day, channel_id: int) -> Manifest:
    m = db.scalar(
        select(Manifest)
        .where(Manifest.dispatch_date == day, Manifest.channel_id == channel_id, Manifest.status == "OPEN")
        .order_by(Manifest.seq.desc())
    )
    if m:
        return m
    # Next number after the CLOSED batches only: stations racing to open today's batch then all pick the same
    # number and the unique key settles it (counting the racer's new open batch too would open a second one).
    last_seq = db.scalar(
        select(func.max(Manifest.seq)).where(Manifest.dispatch_date == day, Manifest.channel_id == channel_id,
                                             Manifest.status == "CLOSED")
    )
    m = Manifest(dispatch_date=day, channel_id=channel_id, seq=(last_seq or 0) + 1)
    db.add(m)
    try:
        db.flush()
    except IntegrityError:
        # Another station opened today's batch for this channel a split-second earlier (first scans of the day
        # at the same moment). Nothing else is written yet in this scan, so start over and use theirs.
        db.rollback()
        m = db.scalar(
            select(Manifest)
            .where(Manifest.dispatch_date == day, Manifest.channel_id == channel_id, Manifest.status == "OPEN")
            .order_by(Manifest.seq.desc())
        )
        if m is None:
            raise
    return m


def _event(db: Session, *, user: User, station: str, channel_id: int | None, raw: str, norm: str, outcome: str,
           message: str, scan_id: int | None = None) -> ScanEvent:
    now = utcnow()
    ev = ScanEvent(
        created_at=now, dispatch_date=dispatch_date_for(now), user_id=user.id, station=station[:60],
        channel_id=channel_id, tracking_raw=raw[:160], tracking_norm=norm[:120], outcome=outcome,
        message=message[:300], scan_id=scan_id,
    )
    db.add(ev)
    return ev


def looks_like_qr(raw: str, norm: str) -> bool:
    """Square QR / DataMatrix codes on the label (URLs, UPI, app links, vCards) are not AWB barcodes.
    They must be rejected as INVALID, never stored as UNVERIFIED scans."""
    v = (raw or "").strip()
    if not v:
        return True
    if any(ch.isspace() for ch in v):
        return True
    low = v.lower()
    if "://" in v:
        return True
    if low.startswith(("http:", "https:", "www.", "upi:", "mailto:", "tel:", "smsto:", "sms:",
                        "geo:", "wifi:", "begin:", "mecard", "vcard")):
        return True
    if len(v) > 60 or len(norm) > MAX_TRACKING_LEN:
        return True
    if norm.startswith(("HTTP", "WWW", "UPI", "VCARD", "MECARD", "WIFI")):
        return True
    return False


def _invalid(db: Session, *, user: User, station: str, channel_id: int, raw: str, norm: str, message: str,
             code: str = "INVALID") -> dict:
    _event(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=norm, outcome="INVALID",
           message=message)
    db.commit()
    return {"severity": "error", "code": code, "message": message}


def _is_marked(scan: Scan | None) -> bool:
    """Recorded by the one-time "already shipped in OMSGuru" mark (services/marking.py), not by a packer."""
    return bool(scan is not None and MARKED_SHIPPED_FLAG in (scan.flags or "").split(","))


# A scan of the same AWB by the same packer in the same channel within this many seconds is their own scan
# repeated (no answer in time / not sure it beeped): answered "already saved", not "DUPLICATE - set aside".
REPEAT_SECONDS = 120


def _duplicate_response(db: Session, existing: Scan, user: User, station: str, channel_id: int, raw: str) -> dict:
    if (existing.user_id == user.id and existing.channel_id == channel_id
            and (utcnow() - existing.scanned_at).total_seconds() < REPEAT_SECONDS):
        msg = f"Already saved - your own scan at {to_local(existing.scanned_at):%H:%M:%S}. Put it with the dispatch."
        _event(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=existing.tracking_norm,
               outcome="REPEAT", message=msg, scan_id=existing.id)
        db.commit()
        return {"severity": "success", "code": "ALREADY_SAVED", "message": msg, "scan": scan_payload(existing),
                "order": order_payload(existing.order)}
    when = to_local(existing.scanned_at).strftime("%d-%b-%Y %H:%M")
    who = (existing.user.full_name or existing.user.username) if existing.user else "?"
    ch = existing.channel.name if existing.channel else ""
    msg = f"DUPLICATE - already scanned on {when} by {who} ({ch})"
    _event(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=existing.tracking_norm,
           outcome="DUPLICATE", message=msg, scan_id=existing.id)
    db.commit()
    payload = {"severity": "error", "code": "DUPLICATE", "message": msg, "scan": scan_payload(existing),
               "order": order_payload(existing.order)}
    hub.publish("scan_rejected", {"code": "DUPLICATE", "channel_id": channel_id, "tracking": raw,
                                  "user": user.full_name or user.username, "message": msg})
    return payload


def process_scan(
    db: Session,
    *,
    user: User,
    channel_id: int,
    raw: str,
    station: str = "",
    on_unknown: Callable[[], None] | None = None,
    live: Callable[[str, str, list[OmsOrder]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw = (raw or "").strip()
    norm = normalize_tracking(raw)
    channel = db.get(Channel, channel_id)
    if not channel:
        return {"severity": "error", "code": "NO_CHANNEL", "message": "Select a valid sales channel first"}

    if looks_like_qr(raw, norm):
        return _invalid(
            db, user=user, station=station, channel_id=channel_id, raw=raw, norm=norm,
            message="QR code detected - scan the AWB barcode (the straight lines), not the square QR code",
        )

    if len(norm) < MIN_TRACKING_LEN:
        return _invalid(
            db, user=user, station=station, channel_id=channel_id, raw=raw, norm=norm,
            message=f"Invalid barcode '{raw[:40]}' - too short for a tracking ID",
        )

    orders = find_orders(db, norm)
    key = _dedupe_key(norm, orders)
    # Duplicates are answered from the database alone - no API credit spent on them.
    existing = db.scalar(select(Scan).where(Scan.tracking_norm == key))
    # A record of the one-time "already shipped in OMSGuru" mark is not a scan: this real scan replaces it (only if
    # it is accepted - see below), so the packer never hears "Duplicate - set aside" for it.
    marked = existing if _is_marked(existing) else None
    if existing and not marked:
        return _duplicate_response(db, existing, user, station, channel_id, raw)

    # The 2-D route code (| / \\ ...) is never an AWB, order id or invoice of an unsynced order: answered without
    # asking OMSGuru. (Known invoice numbers with "/" were already found locally above.)
    if not orders and awb_shapes.has_symbols(raw):
        why = awb_shapes.wrong_barcode(db, channel_id, raw, norm, channel.name)
        if why:
            return _invalid(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=norm,
                            message=why, code="WRONG_BARCODE")

    # Live fetch from OMSGuru for fresh status/details (falls back to the local copy if busy).
    live_info: dict[str, Any] = {"live": "off"}
    if live:
        # Hand the connection back while OMSGuru is asked (up to LIVE_TIMEOUT_SECONDS): the lookup uses its own
        # sessions, and holding two connections per scan ran the pool dry at 15+ stations (load test 3 Oct 2026).
        db.commit()
        try:
            live_info = live(raw, norm, orders)
        except Exception:  # noqa: BLE001 - a lookup problem must never block dispatch
            log.exception("live lookup failed for %s", raw)
            live_info = {"live": "error"}
        if live_info.get("changed"):
            db.expire_all()
            orders = find_orders(db, norm)
            new_key = _dedupe_key(norm, orders)
            if new_key != key:
                key = new_key
                existing = db.scalar(select(Scan).where(Scan.tracking_norm == key))
                marked = existing if _is_marked(existing) else None
                if existing and not marked:
                    linked = _linked_response(db, existing, user, station, channel_id, raw)
                    return linked or _duplicate_response(db, existing, user, station, channel_id, raw)

    # Still unknown after asking OMSGuru (which finds new AWBs, new courier formats and order / sub-order ids of
    # orders not synced yet) and not shaped like this channel's AWBs: another barcode on the label or packet -
    # rejected instead of being saved as "Not found".
    if not orders:
        why = awb_shapes.wrong_barcode(db, channel_id, raw, norm, channel.name)
        if why:
            return _invalid(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=norm,
                            message=why, code="WRONG_BARCODE")

    awbs = sorted({o.tracking_norm for o in orders if o.tracking_norm})
    if len(awbs) > 1 and norm not in awbs:
        msg = (f"Order {orders[0].channel_order_id} has {len(awbs)} shipments in OMS ({', '.join(awbs[:4])}) - "
               "scan the AWB barcode on this packet instead")
        _event(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=norm, outcome="INVALID", message=msg)
        db.commit()
        return {"severity": "error", "code": "AMBIGUOUS", "message": msg, "live": live_info.get("live"), "live_ms": live_info.get("ms")}

    channels = {c.id: c for c in db.scalars(select(Channel))}
    verdict = evaluate(orders, channel_id, channels)
    primary = orders[0] if orders else None

    if verdict.result == "BLOCK":
        _event(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=key,
               outcome=verdict.outcome, message=verdict.message)
        db.commit()
        hub.publish("scan_rejected", {"code": verdict.outcome, "channel_id": channel_id, "tracking": raw,
                                      "user": user.full_name or user.username, "message": verdict.message})
        return {"severity": "error", "code": verdict.flags[0] if verdict.flags else verdict.outcome,
                "message": verdict.message, "order": order_payload(primary), "live": live_info.get("live"), "live_ms": live_info.get("ms")}

    if marked is not None:  # accepted: the real scan takes the place of the "shipped in OMSGuru" mark
        mark = db.get(Scan, marked.id)  # re-read: another station may have replaced it meanwhile
        if mark is not None and _is_marked(mark):
            _event(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=mark.tracking_norm,
                   outcome="MARK_REPLACED", scan_id=mark.id,
                   message=f"Real scan replaces the 'shipped in OMSGuru' mark dated {mark.dispatch_date:%d-%b-%Y}")
            db.delete(mark)
            db.flush()
    now = utcnow()
    day = dispatch_date_for(now)
    manifest = _open_manifest(db, day, channel_id)
    scan = Scan(
        tracking_norm=key, tracking_raw=(primary.tracking_raw if primary and primary.tracking_raw else raw)[:160],
        dispatch_date=day, scanned_at=now, user_id=user.id, station=station[:60], channel_id=channel_id,
        order_id=primary.id if primary else None, order_json=_snapshot(primary), manifest_id=manifest.id,
        result=verdict.result,
        flags=",".join(verdict.flags), message=verdict.message[:300],
    )
    db.add(scan)
    try:
        db.flush()
    except IntegrityError:
        # Another station saved the same AWB a split-second earlier.
        db.rollback()
        existing = db.scalar(select(Scan).where(Scan.tracking_norm == key))
        if existing:
            return _duplicate_response(db, existing, user, station, channel_id, raw)
        raise
    outcome = {"OK": "ACCEPTED", "WARN": "WARN", "UNVERIFIED": "UNVERIFIED"}[verdict.result]
    _event(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=key, outcome=outcome,
           message=verdict.message, scan_id=scan.id)
    db.commit()
    db.refresh(scan)

    if verdict.result == "UNVERIFIED" and on_unknown:
        on_unknown()

    payload = scan_payload(scan)
    hub.publish("scan", payload)
    severity = {"OK": "success", "WARN": "warning", "UNVERIFIED": "warning"}[verdict.result]
    code = {"OK": "OK", "WARN": verdict.flags[0] if verdict.flags else "WARN", "UNVERIFIED": "NOT_IN_OMS"}[verdict.result]
    return {"severity": severity, "code": code, "message": verdict.message, "scan": payload, "order": payload["order"],
            "live": live_info.get("live"), "live_ms": live_info.get("ms")}


def _dedupe_key(norm: str, orders: list[OmsOrder]) -> str:
    """If an order / invoice barcode was scanned, dedupe on that order's AWB instead."""
    awbs = {o.tracking_norm for o in orders if o.tracking_norm}
    if orders and norm not in awbs and len(awbs) == 1:
        return next(iter(awbs))
    return norm


def _linked_response(db: Session, existing: Scan, user: User, station: str, channel_id: int, raw: str) -> dict | None:
    """The barcode just scanned (e.g. the order id) resolved an earlier UNVERIFIED scan of the same packet."""
    just_resolved = existing.resolved_at is not None and (utcnow() - existing.resolved_at).total_seconds() < 60
    fresh_alert = existing.alert.startswith("AFTER SCAN") and existing.result == "UNVERIFIED"
    if not (just_resolved or fresh_alert):
        return None
    payload = scan_payload(existing)
    if existing.alert:
        msg = existing.alert.replace("AFTER SCAN: ", "")
        severity, code = "error", "ALERT"
    else:
        msg = f"Order found in OMSGuru - the earlier scan of {existing.tracking_raw} is now {existing.result}"
        severity, code = ("success" if existing.result == "OK" else "warning"), "LINKED"
    _event(db, user=user, station=station, channel_id=channel_id, raw=raw, norm=existing.tracking_norm,
           outcome="ACCEPTED" if severity != "error" else "BLOCKED", message=msg, scan_id=existing.id)
    db.commit()
    return {"severity": severity, "code": code, "message": msg, "scan": payload, "order": payload["order"], "live": "fresh"}


RETIRED_FLAG = "ALREADY_SHIPPED_IN_OMS"
_RETIRED_NOTE = "OMS already shows this order as shipped"
# flag -> the note it added to the message; both stopped being a Check (5 Oct and 8 Oct 2026)
RETIRED_CHECKS = {RETIRED_FLAG: _RETIRED_NOTE,
                  "STATUS_CHANGED": "Order is no longer Ready-to-ship in OMS - verify status"}


def clear_shipped_checks(db: Session) -> int:
    """Scans saved while "already shipped in OMS" / "no longer Ready-to-ship" was still a Check: drop that flag;
    a scan with no other reason left is OK. Idempotent - runs at every start, finds nothing once done."""
    fixed = 0
    retired = or_(*(Scan.flags.like(f"%{flag}%") for flag in RETIRED_CHECKS))
    for scan in db.scalars(select(Scan).where(Scan.result == "WARN", retired)):
        flags = [f for f in (scan.flags or "").split(",") if f and f not in RETIRED_CHECKS]
        notes = [n for n in (scan.message or "").split("; ") if n and n not in RETIRED_CHECKS.values()]
        scan.flags = ",".join(flags)
        if flags:
            scan.message = "; ".join(notes)[:300]
        else:
            scan.result, scan.message = "OK", "Verified"
        fixed += 1
    if fixed:
        db.commit()
        cache.clear()
    return fixed


def reverify_scans(db: Session, tracking_norms: set[str]) -> list[dict[str, Any]]:
    """Re-check stored scans after an OMS sync: resolve UNVERIFIED ones and raise alerts on bad news."""
    if not tracking_norms:
        return []
    changed: list[dict[str, Any]] = []
    channels = {c.id: c for c in db.scalars(select(Channel))}
    norms = list(tracking_norms)
    for i in range(0, len(norms), 500):
        for scan in db.scalars(select(Scan).where(Scan.tracking_norm.in_(norms[i:i + 500]))).unique():
            orders = list(db.scalars(select(OmsOrder).where(OmsOrder.tracking_norm == scan.tracking_norm)).unique())
            if not orders:
                continue
            verdict = evaluate(orders, scan.channel_id, channels)
            before = (scan.result, scan.alert, scan.order_id)
            scan.order = orders[0]
            scan.order_json = _snapshot(orders[0])
            if verdict.result == "BLOCK":
                scan.alert = ("AFTER SCAN: " + verdict.message)[:300]
            elif scan.alert.startswith("AFTER SCAN"):
                scan.alert = ""
            if verdict.result != "BLOCK":
                if scan.result == "UNVERIFIED":
                    scan.resolved_at = utcnow()
                scan.result = verdict.result
                scan.flags = ",".join(verdict.flags)
                scan.message = verdict.message[:300]
            if (scan.result, scan.alert, orders[0].id) != before:
                changed.append(scan_payload(scan))
    if changed:
        db.commit()
        for p in changed:
            cache.invalidate(p["channel_id"])
            hub.publish("scan_updated", p)
    return changed
