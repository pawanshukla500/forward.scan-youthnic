"""Is a scanned code an AWB at all?

Every sales channel's AWBs come in a few fixed formats - written as a "shape" with letters -> A and digits -> 9:
Flipkart FMPC6580571822 -> AAAA9999999999, Myntra MYSP1479432935 -> AAAA9999999999, Amazon 374012345678 ->
999999999999. The shapes are learned from the orders synced from OMSGuru (no hand-made list, so a new courier is
learned as soon as its orders sync). A code that is not in OMSGuru AND does not have one of the channel's shapes is
not an AWB: the packer scanned another barcode on the label or packet. It is rejected ("scan the AWB barcode")
instead of being saved as "Not found", which filled the dispatch report with blank rows.

Measured on production, 8 Oct 2026: 0 of 16,147 found scans fall outside their channel's shapes; 261 of the 303
"Not found" scans were other barcodes - Myntra packet ids (MPP3EM...), Flipkart SPTSB..., Ajio DB... bag ids,
product EAN codes on the polybag, and the 2-D route code (5|\\MB-2141...|O|NAG/WRA|S|E|03|...).
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import OmsOrder
from ..oms.mapping import _SUFFIX_RE

MIN_ORDERS = 100     # a channel needs this many synced AWBs before its formats are trusted
MIN_SEEN = 3         # a format must occur this often to count (one odd row is not a format)
CACHE_SECONDS = 600  # formats change only when a new courier starts

_SYMBOLS = re.compile(r"[^A-Za-z0-9_-]")
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "formats": None}


def shape(norm: str) -> str:
    return re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", (norm or "").upper()))


def has_symbols(raw: str) -> bool:
    """AWBs are letters, digits, '-' and '_' only. | / \\ = % " : come from the 2-D route code or a bad read."""
    return bool(_SYMBOLS.search(_SUFFIX_RE.sub("", raw or "").strip()))


def describe(example: str) -> str:
    """FMPC6580571822 -> "FMPC + 10 digits"; 374012345678 -> "12 digits"; AM123456789IN -> "AM + 9 digits + IN"."""
    m = re.fullmatch(r"([A-Z]*)(\d+)([A-Z]*)", example)
    if not m:
        return f"{len(example)} characters like {example[:4]}..."
    pre, digits, post = m.groups()
    parts = ([pre] if pre else []) + [f"{len(digits)} digits"] + ([post] if post else [])
    return " + ".join(parts)


def formats(db: Session) -> dict[int, dict[str, tuple[int, str]]]:
    """channel id -> {shape: (orders, an example AWB)} for channels with enough synced AWBs."""
    with _lock:
        if _cache["formats"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["formats"]
    counts: dict[int, Counter] = {}
    examples: dict[tuple[int, str], str] = {}
    rows = db.execute(select(OmsOrder.tracking_norm, OmsOrder.channel_id)
                      .where(OmsOrder.tracking_norm != "", OmsOrder.channel_id.is_not(None)))
    for norm, cid in rows:
        s = shape(norm)
        counts.setdefault(cid, Counter())[s] += 1
        examples.setdefault((cid, s), norm)
    learned = {
        cid: {s: (n, examples[(cid, s)]) for s, n in cnt.items() if n >= MIN_SEEN}
        for cid, cnt in counts.items() if sum(cnt.values()) >= MIN_ORDERS
    }
    with _lock:
        _cache.update(at=time.monotonic(), formats=learned)
    return learned


def reset() -> None:
    with _lock:
        _cache.update(at=0.0, formats=None)


def wrong_barcode(db: Session, channel_id: int, raw: str, norm: str, channel_name: str = "") -> str | None:
    """Why this code is not an AWB of the channel (the message for the packer), or None if it may be one.
    Only asked for codes OMSGuru does not know - a code that matches an order is never rejected here."""
    if has_symbols(raw):
        return ("WRONG BARCODE - this is the route / 2-D code, not the AWB. "
                "Scan the AWB barcode (the long one with the tracking number under it)")
    known = formats(db).get(channel_id)
    if not known or shape(norm) in known:
        return None
    top = sorted(known.values(), reverse=True)[:2]
    looks = " or ".join(describe(ex) for _, ex in top)
    where = f"{channel_name} AWBs" if channel_name else "AWBs of this marketplace"
    return (f"WRONG BARCODE - {raw[:30]} is not an AWB. {where} look like {looks}. "
            "Scan the AWB barcode on the label (not the packet / product / route code)")
