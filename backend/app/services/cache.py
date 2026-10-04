"""Short-lived in-process cache for the numbers every open page re-reads after each scan.

After a scan, every station of that channel (and every dashboard) refetches at the same moment. Two rules keep that
cheap (25-station load test, 3 Oct 2026: from CPU-bound collapse to 503 scans/min, p99 0.17 s):
  * single flight - while one request computes an answer, the others for the same key wait for it and share it;
  * at most one recompute per `min_interval` per key - a scan marks the answer stale, but within that interval the
    last answer is served (it is at most that old). Pages can compare the answer's own timestamp with their last
    scan and ask again a moment later.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable

_lock = threading.Lock()
_versions: dict[Hashable, int] = {}
_data: dict[Hashable, tuple[float, int, Any]] = {}  # key -> (computed at, scope version, value)
_inflight: dict[Hashable, threading.Event] = {}

ALL = "*"  # scope of answers that cover every channel (summaries)


def invalidate(channel_id: int | None = None) -> None:
    """A scan on this channel changed: its own answers and the all-channel answers are stale."""
    with _lock:
        for scope in {channel_id, ALL} - {None}:
            _versions[scope] = _versions.get(scope, 0) + 1


def cached(key: Hashable, scope: Hashable, ttl: float, compute: Callable[[], Any], min_interval: float = 0.0) -> Any:
    """The value for key: reused while fresh (same scope version and younger than ttl) or younger than
    min_interval; otherwise computed once and shared with everyone asking at the same time."""
    while True:
        with _lock:
            now = time.monotonic()
            version = _versions.get(scope, 0)
            hit = _data.get(key)
            if hit and (now - hit[0] < min_interval or (now - hit[0] < ttl and hit[1] == version)):
                return hit[2]
            waiting = _inflight.get(key)
            if waiting is None:
                _inflight[key] = done = threading.Event()
                break
        waiting.wait(timeout=30)  # someone else is computing it: use their answer (loop re-checks)
        with _lock:
            hit = _data.get(key)
            if hit and time.monotonic() - hit[0] < max(ttl, min_interval, 1.0):
                return hit[2]
    try:
        value = compute()
        with _lock:
            _data[key] = (now, version, value)
            if len(_data) > 500:  # keys are few in practice; keep it that way
                for k in sorted(_data, key=lambda k: _data[k][0])[:250]:
                    del _data[k]
        return value
    finally:
        with _lock:
            _inflight.pop(key, None)
        done.set()


def clear() -> None:
    with _lock:
        _data.clear()
