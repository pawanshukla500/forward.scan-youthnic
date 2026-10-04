"""The admin-set date orders are counted from.

Only AWBs generated (orders packed / made ready to ship) on or after this dispatch day count as generated, pending,
overdue or reconciled, and the OMSGuru history fill does not reach back before it. Older orders that are still open in
OMSGuru stay in the local copy, so a packet from before the date can still be scanned - it is just not counted.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime

from ..db import session_scope
from ..models import SyncState
from ..timeutil import day_bounds_utc

KEY = "tracking_start"
_cache: tuple[float, date | None] | None = None


def start_date() -> date | None:
    global _cache
    if _cache and time.monotonic() - _cache[0] < 5:
        return _cache[1]
    with session_scope() as db:
        row = db.get(SyncState, KEY)
        raw = json.loads(row.value) if row and row.value else None
    value = date.fromisoformat(raw) if raw else None
    _cache = (time.monotonic(), value)
    return value


def start_utc() -> datetime | None:
    """Start of the tracking start day (honours DAY_START_HOUR), as naive UTC like the stored timestamps."""
    d = start_date()
    return day_bounds_utc(d)[0] if d else None


def set_start(d: date | None) -> None:
    global _cache
    with session_scope() as db:
        row = db.get(SyncState, KEY)
        value = json.dumps(d.isoformat() if d else None)
        if row is None:
            db.add(SyncState(key=KEY, value=value))
        else:
            row.value = value
    _cache = None
