"""Turn OMSGuru order/invoice rows into OmsOrder column values."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from ..models import Channel
from ..timeutil import from_unix

_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*")
_TRACK_KEEP = re.compile(r"[^A-Z0-9\-_/]")


def normalize_tracking(raw: str | None) -> str:
    """Canonical form used for duplicate checks and AWB lookups.
    Strips courier suffixes like '( EK_E2E )' before filtering."""
    if not raw:
        return ""
    s = _SUFFIX_RE.sub(" ", str(raw)).strip().upper()
    s = _TRACK_KEEP.sub("", s)
    return s.strip("-_/")


def normalize_sku_key(val: Any) -> str:
    """Normalize SKU for indexing and joins: uppercase, whitespace collapsed."""
    if not val:
        return ""
    return re.sub(r"\s+", " ", str(val)).strip().upper()


def normalize_image_url(val: Any) -> str | None:
    if not val or not isinstance(val, str):
        return None
    url = val.strip()
    if not url:
        return None
    if url.startswith("//"):
        return f"https:{url}"
    if url.lower().startswith("http://"):
        return "https://" + url[7:]
    return url


def _norm_label(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# Item-status text -> bucket. Checked in this order (return before cancel: "Cancelled Return Received").
_RETURN = ("return",)
_CANCEL = ("cancel", "removed before shipping")
_SHIPPED = ("shipped", "in transit", "intransit", "delivered", "dispatched", "out for delivery")
_NOT_PACKED = ("new", "pending")
_READY = ("packed", "ready to ship", "readytoship", "rts", "processing")


def item_bucket(status: str | None) -> str:
    s = _norm_label(status)
    if not s:
        return "UNKNOWN"
    if any(k in s for k in _RETURN):
        return "RETURN"
    if any(k in s for k in _CANCEL):
        return "CANCELLED"
    if any(k in s for k in _SHIPPED):
        return "SHIPPED"
    if any(s == k or s.startswith(k) for k in _READY):
        return "OPEN"
    if any(s == k or s.startswith(k) for k in _NOT_PACKED):
        return "NOT_PACKED"
    return "UNKNOWN"


def status_group(statuses: Iterable[str]) -> str:
    buckets = [item_bucket(s) for s in statuses]
    if not buckets:
        return "UNKNOWN"
    live = [b for b in buckets if b != "CANCELLED"]
    if not live:
        return "CANCELLED"
    if "RETURN" in live:
        return "RETURN"
    if "SHIPPED" in live and all(b == "SHIPPED" for b in live):
        return "SHIPPED"
    if len(live) < len(buckets):
        return "PARTIAL_CANCEL"
    if "NOT_PACKED" in live:
        return "NOT_PACKED"
    if "OPEN" in live:
        return "OPEN"
    if "SHIPPED" in live:
        return "SHIPPED"
    return "UNKNOWN"


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_order_row(row: dict[str, Any], source: str) -> dict[str, Any]:
    items = row.get("order_items") or []
    if isinstance(items, dict):
        items = list(items.values())
    items = [i for i in items if isinstance(i, dict)]

    order_ids = [str(i.get("channel_order_id") or "").strip() for i in items]
    order_ids = [o for o in order_ids if o] or [str(row.get("channel_order_id") or "").strip()]
    sub_ids = sorted({str(i.get("channel_sub_order_id") or "").strip() for i in items} - {""})
    if not sub_ids and row.get("channel_sub_order_id"):
        sub_ids = [str(row["channel_sub_order_id"]).strip()]
    statuses = [str(i.get("status") or "") for i in items]

    tracking_raw = str(row.get("shipment_tracker") or row.get("shipment_tracking_id") or "").strip()
    channel_label = str(row.get("channel") or "").strip()
    invoice_id = str(row.get("invoice_id") or "").strip()
    channel_order_id = order_ids[0] if order_ids else ""

    compact_items = [
        {
            "sku": str(i.get("sku_code") or i.get("sku") or i.get("product_sku") or i.get("sku_name") or "").strip(),
            "title": str(i.get("title") or i.get("name") or i.get("product_name") or i.get("item_name") or "").strip() or None,
            "qty": int(_num(i.get("qty"))),
            "sub_order_id": str(i.get("channel_sub_order_id") or i.get("sub_order_id") or "").strip(),
            "status": str(i.get("status") or "").strip(),
            "amount": round(_num(i.get("invoice_amount")), 2),
            "image_url": normalize_image_url(i.get("img_url") or i.get("image_url") or i.get("imageUrl") or i.get("image")),
        }
        for i in items
    ]

    key_parts = [channel_label, channel_order_id, invoice_id or ",".join(sub_ids), normalize_tracking(tracking_raw)]
    try:
        last_id = int(row.get("last_id")) if row.get("last_id") not in (None, "") else None
    except (TypeError, ValueError):
        last_id = None

    return {
        "oms_key": "|".join(key_parts)[:300],
        "oms_last_id": last_id,
        "channel_label": channel_label,
        "company": str(row.get("company") or "").strip(),
        "warehouse": str(row.get("warehouse") or "").strip(),
        "channel_order_id": channel_order_id,
        "sub_order_ids": ",".join(sub_ids),
        "invoice_id": invoice_id,
        "invoice_date": from_unix(row.get("invoice_date")),
        "order_date": from_unix(row.get("order_date")),
        "sla_date": from_unix(row.get("sla_date")),
        "shipment_date": from_unix(row.get("shipment_date")),
        "tracking_raw": tracking_raw,
        "tracking_norm": normalize_tracking(tracking_raw),
        "shipping_company": str(row.get("shipping_company") or "").strip(),
        "order_type": str(row.get("order_type") or "").strip(),
        "buyer_name": str(row.get("buyer_name") or "").strip(),
        "buyer_city": str(row.get("buyer_city") or "").strip(),
        "buyer_state": str(row.get("buyer_state") or "").strip(),
        "buyer_pincode": str(row.get("buyer_pincode") or "").strip(),
        "item_count": len(items),
        "total_qty": sum(ci["qty"] for ci in compact_items),
        "total_amount": round(sum(_num(i.get("invoice_amount")) for i in items), 2),
        "currency": str((items[0].get("currency_code") if items else "") or "INR"),
        "items_json": json.dumps(compact_items, ensure_ascii=False),
        "status_text": ", ".join(sorted({s for s in statuses if s}))[:200],
        "status_group": status_group(statuses),
        "source": source,
    }


class ChannelMatcher:
    """Maps the free-text `channel` field on an order to a Channel (sales channel) id."""

    def __init__(self, channels: Iterable[Channel]):
        self._exact: dict[str, int] = {}
        self._pairs: dict[tuple[str, str], list[Channel]] = {}
        chans = sorted(channels, key=lambda c: (c.oms_status != "active", c.id))
        for c in chans:
            for label in [c.name, *[a for a in (c.aliases or "").splitlines() if a.strip()]]:
                self._exact.setdefault(_norm_label(label), c.id)
            self._pairs.setdefault((_norm_label(c.marketplace), _norm_label(c.company)), []).append(c)

    def match(self, label: str, company: str = "") -> int | None:
        nl = _norm_label(label)
        if not nl:
            return None
        if nl in self._exact:
            return self._exact[nl]
        comp = _norm_label(company)
        parts = [p.strip() for p in nl.split(" - ") if p.strip()]
        candidates: list[tuple[str, str]] = []
        if len(parts) >= 2:
            left, right = parts[0], " - ".join(parts[1:])
            candidates += [(left, right), (right, left)]
            left2, right2 = " - ".join(parts[:-1]), parts[-1]
            candidates += [(left2, right2), (right2, left2)]
        if comp:
            candidates.append((nl, comp))
            for p in parts:
                candidates.append((p, comp))
        for pair in candidates:
            if pair in self._pairs:
                return self._pairs[pair][0].id
        return None
