"""Background OMSGuru sync.

What is synced continuously: orders that are Packed or Ready-to-ship (they can be dispatched).
What is stored: every order whose AWB was generated in the last RETAIN_ORDERS_DAYS, so each channel's
"AWBs generated vs scanned vs pending" and the duplicate / cancel checks have full data. Once an order
leaves Packed / Ready-to-ship it is no longer synced - its last known status is kept until it ages out.
Orders cancelled before an AWB existed are never stored.

  * invoices      - incremental, every SYNC_INTERVAL_SECONDS (a new invoice = a new AWB/label)
  * open orders   - full refresh of Ready-to-ship + Packed orders (the source of truth for "pending")
  * cancellations - recent cancellations, applied ONLY to orders already stored
  * crosscheck    - after every full refresh: OMSGuru's own pending report (order_aging) vs the local copy
  * exit check    - orders that left Packed / Ready-to-ship unscanned: what are they now? (spare credits only)
  * history       - one-time backfill of the previous days' AWBs, using only spare API credits
  * audit         - hourly order-trail check: re-reads EVERY invoice of today's and yesterday's dispatch day and
                    compares it AWB by AWB with the local copy; adds what is missing, updates statuses, and keeps
                    "OMSGuru N AWBs = N here" per channel for Admin / Pending (spare API credits only)
  * cleanup       - drops orders older than RETAIN_ORDERS_DAYS, and scans older than SCAN_RETENTION_DAYS
  * channels      - sales-channel list, every 6h
Scans keep their own copy of the order details, so pruning orders never loses dispatch history.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import exists, func, select, update

from ..config import settings
from ..db import optimize, session_scope
from ..models import Channel, OmsOrder, Scan, SkuPhoto, SyncLog, SyncState, Warehouse
from ..services import cache, tracking
from ..services.realtime import hub
from ..services.scanning import find_orders, reverify_scans
from ..timeutil import day_bounds_utc, today_dispatch_date, utcnow
from datetime import timezone
from .client import OmsAuthError, OmsBusy, OmsClient, OmsDownError, OmsError, OmsThrottled
from .mapping import ChannelMatcher, normalize_image_url, normalize_sku_key, normalize_tracking, parse_order_row
from .mock import MockOmsClient

log = logging.getLogger("oms.sync")

STATUS_READY_TO_SHIP = 1
STATUS_PACKED = 9
# Fetched in the order orders move (Packed -> Ready to ship): an order that moves on mid-refresh is then still
# seen once. The other way round it would be missed by both lists and wrongly marked as left.
OPEN_STATUSES = (STATUS_PACKED, STATUS_READY_TO_SHIP)
# order_aging status names that make up the working set
AGING_OPEN = ("packed", "ready to ship")
CANCEL_STATUSES = (15, 6, 19, 20)  # Cancelled, CancelInit, Cancelled Before Shipping, Removed Before Shipping
INVOICE_OVERLAP_SECONDS = 15 * 60
# Background history backfill only runs while at least this many API credits are free (idle capacity).
HISTORY_MIN_CREDITS = 30
# A warehouse is synced automatically when it holds at least this share of recent orders
# (skips marketplace fulfilment centres such as Amazon FBA / Flipkart FA you do not dispatch from).
WAREHOUSE_MIN_SHARE = 0.05
CHANNELS_EVERY_SECONDS = 6 * 3600
CLEANUP_EVERY_SECONDS = 15 * 60
SYNC_LOG_DAYS = 14
# Old scans / events are deleted this many rows per transaction (see prune_scans).
PRUNE_BATCH = 2000
# Exit check: orders looked up per run, and the pause when there is nothing to look up.
EXIT_CHECK_BATCH = 10
EXIT_CHECK_IDLE_SECONDS = 120
# A pending AWB OMSGuru already shows shipped is asked about again this often (cancelled / returned later?).
EXIT_RECHECK_HOURS = 12
# A lookup method that missed this often for a channel, and never found anything, is skipped for it.
LOOKUP_GIVE_UP = 3
# Order-trail audit: every invoice of the last AUDIT_DAYS dispatch days, re-read every AUDIT_EVERY_SECONDS.
AUDIT_EVERY_SECONDS = 3600
AUDIT_DAYS = 2
# Once a night (first round after AUDIT_DEEP_HOUR, local time) the audit covers this many days instead: late
# cancellations / returns of older pending AWBs, and AWBs OMSGuru added to earlier days afterwards.
AUDIT_DEEP_DAYS = 7
AUDIT_DEEP_HOUR = 3
# OMSGuru unreachable at least this long (the new-AWB sync failing): when it answers again, a full catch-up starts.
CATCH_UP_AFTER_SECONDS = 5 * 60
# While OMSGuru is down only the new-AWB sync keeps knocking, this often at most (other jobs wait; scans answer
# from the stored orders without asking OMSGuru live).
DOWN_PROBE_SECONDS = 30
# What counts as "OMSGuru is unavailable" (outage mode, banner, incident history). Throttling (429) is not.
OUTAGE_KINDS = ("down", "key_refused")
INCIDENTS_KEPT = 30
AUDIT_KEEP_DAYS = 10  # results kept for Admin / Pending
# Status groups that belong in the working set (see mapping.status_group).
WORKING_SET = ("OPEN",)

# Fixed categorical order (CVD-validated); channels past slot 8 get neutral grey - identity is always
# carried by the channel name too, never by colour alone.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
NEUTRAL = "#898781"


@dataclass
class JobStatus:
    name: str
    last_started: float | None = None
    last_finished: float | None = None
    last_ok: bool | None = None
    last_message: str = ""
    running: bool = False
    records: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("name", "last_started", "last_finished", "last_ok", "last_message", "running", "records")}


@dataclass
class _Job:
    status: JobStatus
    calls: int = 0
    records: int = 0
    started: float = field(default_factory=time.time)


def _get_state(key: str, default: Any = None) -> Any:
    with session_scope() as db:
        row = db.get(SyncState, key)
        if not row or row.value == "":
            return default
        try:
            return json.loads(row.value)
        except ValueError:
            return default


def _set_state(key: str, value: Any) -> None:
    with session_scope() as db:
        row = db.get(SyncState, key)
        if row is None:
            db.add(SyncState(key=key, value=json.dumps(value)))
        else:
            row.value = json.dumps(value)


async def _aget(key: str, default: Any = None) -> Any:
    """_get_state for async code: the database is read on a worker thread, not on the event loop."""
    return await asyncio.to_thread(_get_state, key, default)


async def _aset(key: str, value: Any) -> None:
    await asyncio.to_thread(_set_state, key, value)


def _log_run(job: str, ok: bool, calls: int, records: int, msg: str) -> None:
    with session_scope() as db:
        db.add(SyncLog(job=job, started_at=utcnow(), finished_at=utcnow(), ok=ok, calls=calls, records=records,
                       message=msg[:1000]))


# job name -> SyncEngine coroutine method
JOB_METHODS = {"channels": "refresh_channels", "sku_photos": "refresh_sku_photos", "invoices": "sync_invoices",
               "open_orders": "step_open_orders", "crosscheck": "crosscheck", "cancel_sweep": "cancel_sweep",
               "exit_check": "exit_check", "history": "step_history", "cleanup": "cleanup", "audit": "step_audit"}


class SyncEngine:
    def __init__(self) -> None:
        self.client: OmsClient | MockOmsClient = MockOmsClient() if settings.oms_use_mock else OmsClient()
        self.jobs = {n: JobStatus(n) for n in ("channels", "sku_photos", "invoices", "open_orders", "crosscheck",
                                               "cancel_sweep", "audit", "exit_check", "history", "cleanup")}
        self._urgent = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stop = False
        self._last_urgent = 0.0
        self._live_pull_lock = threading.Lock()
        self._last_live_pull = 0.0
        self.live_stats: Counter[str] = Counter()
        # (channel id, "sub" | "order", "hit" | "miss") -> count, and the method that last found each channel's shipment
        self.lookup_stats: Counter[tuple[int | None, str, str]] = Counter()
        self._lookup_pref: dict[int | None, str] = {}
        # monotonic time of the last scheduler loop turn (/api/health; the server watchdog restarts a stalled loop)
        self.heartbeat = time.monotonic()
        # time.time() since when OMSGuru has not been answering (None = up); kept in the sync state as well
        self.omsguru_down_since: float | None = None

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        if settings.sync_enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name="oms-sync")

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.client.aclose()

    def request_urgent(self) -> None:
        """Called (from any thread) when a scan misses the cache. Debounced to protect API credits."""
        now = time.time()
        if now - self._last_urgent < 20:
            return
        self._last_urgent = now
        loop = hub._loop  # noqa: SLF001 - same loop the app runs on
        if loop:
            loop.call_soon_threadsafe(self._urgent.set)

    def request_full(self, job: str) -> None:
        if job == "open_orders":
            _set_state("open_orders_progress", None)
            _set_state("open_orders_last_done", 0)
        elif job == "cancel_sweep":
            _set_state("cancel_sweep_last_done", 0)
        elif job == "channels":
            _set_state("channels_last_done", 0)
        elif job == "sku_photos":
            _set_state("sku_photos_last_done", 0)
        elif job == "crosscheck":
            _set_state("crosscheck_due", True)
        elif job == "exit_check":
            _set_state("exit_check_idle_at", 0)
        elif job == "cleanup":
            _set_state("cleanup_last_done", 0)
        elif job == "history":
            _set_state("history_backfill", None)
            _set_state("history_done", False)
        elif job == "audit":
            _set_state("trail_audit_progress", None)
            _set_state("trail_audit_last_done", 0)
        loop = hub._loop  # noqa: SLF001
        if loop:
            loop.call_soon_threadsafe(self._urgent.set)

    def status(self) -> dict[str, Any]:
        with session_scope() as db:
            cached = db.scalar(select(func.count(OmsOrder.id))) or 0
            open_cnt = db.scalar(select(func.count(OmsOrder.id)).where(OmsOrder.status_group == "OPEN")) or 0
            left_cnt = cached - open_cnt
            exit_pending = db.scalar(select(func.count(OmsOrder.id)).where(*_exit_check_filter())) or 0
            chan_names = {c.id: c.name for c in db.scalars(select(Channel))}
            logs = [
                {"job": l.job, "started_at": l.started_at.isoformat() + "Z", "ok": l.ok, "calls": l.calls,
                 "records": l.records, "message": l.message}
                for l in db.scalars(select(SyncLog).order_by(SyncLog.id.desc()).limit(20))
            ]
        return {
            "mode": "mock" if settings.oms_use_mock else "live",
            "enabled": settings.sync_enabled,
            "interval_seconds": settings.sync_interval_seconds,
            "lookback_days": settings.order_lookback_days,
            "jobs": [j.as_dict() for j in self.jobs.values()],
            "limiter": self.client.state.snapshot(),
            "cached_orders": cached,
            "cached_open_orders": open_cnt,
            "cached_left_orders": left_cnt,
            "retain_orders_days": settings.retain_orders_days,
            "scanned_orders_retention_days": settings.scanned_orders_retention_days,
            "scan_retention_days": settings.scan_retention_days,
            "history": _get_state("history_backfill") or {"done": bool(_get_state("history_done"))},
            "invoices_cursor": _get_state("invoices_cursor"),
            "open_orders_progress": _get_state("open_orders_progress"),
            "logs": logs,
            "omsguru": {**(_get_state("omsguru_status") or {"state": "ok"}),
                        "down_since": self.omsguru_down_since,
                        "incidents": list(reversed(_get_state("omsguru_incidents") or []))[:10]},
            "live_lookup": {"enabled": settings.live_lookup, "timeout_seconds": settings.live_timeout_seconds,
                            "outcomes": dict(self.live_stats), "methods": self._lookup_summary(chan_names)},
            "crosscheck": _get_state("crosscheck"),
            "exit_check_pending": exit_pending,
            "trail_audit": _get_state("trail_audit"),
            "trail_audit_progress": _get_state("trail_audit_progress"),
        }

    def _lookup_summary(self, names: dict[int, str]) -> list[dict[str, Any]]:
        """Per channel: which order_details lookup finds its shipments (hits / misses since server start)."""
        out: dict[int | None, dict[str, Any]] = {}
        for (cid, method, kind), n in self.lookup_stats.items():
            row = out.setdefault(cid, {"channel_id": cid, "name": names.get(cid, "Unmapped") if cid else "Unmapped",
                                       "best": self._lookup_pref.get(cid), "sub": [0, 0], "order": [0, 0]})
            row[method][0 if kind == "hit" else 1] += n
        return sorted(out.values(), key=lambda r: r["name"])

    def brief(self) -> dict[str, Any]:
        """Small status for scan stations: is the order list still loading, are we starved of API credits?"""
        has_wh = bool(self._warehouse_ids())
        initial = not _get_state("invoices_cursor") or (has_wh and not _get_state("open_orders_last_done"))
        with session_scope() as db:
            cached = db.scalar(select(func.count(OmsOrder.id))) or 0
        lim = self.client.state
        with session_scope() as db:
            whs = [w.name or str(w.id) for w in db.scalars(select(Warehouse).where(Warehouse.sync_enabled.is_(True)))]
        inv = self.jobs["invoices"]
        check = _get_state("crosscheck") or {}
        return {
            "mode": "mock" if settings.oms_use_mock else "live",
            "warehouses": whs,
            "initial_load": bool(initial),
            "cached_orders": cached,
            # last time new AWBs were pulled successfully (survives restarts via the sync state)
            "last_invoice_sync": _get_state("invoices_last_done"),
            "sync_interval_seconds": settings.sync_interval_seconds,
            "invoices_failing": inv.last_ok is False or bool(self.omsguru_down_since),
            # since when OMSGuru is not answering (unix seconds) - their outage: the banner says so
            "omsguru_down_since": self.omsguru_down_since,
            # ok / down (their outage) / key_refused (our API key is not accepted any more)
            "omsguru_state": (_get_state("omsguru_status") or {}).get("state", "ok"),
            "invoices_error": inv.last_message if inv.last_ok is False else "",
            "waiting_for_credit": lim.waiting,
            "throttled_total": lim.throttled_total,
            "last_error": lim.current_error(),  # only while nothing has succeeded since
            # pending orders differ from OMSGuru's own report even after an early re-refresh
            "crosscheck_off": check.get("ok") is False and not check.get("retry"),
        }

    # ---- scheduler ---------------------------------------------------------------------------

    async def _run(self) -> None:
        await asyncio.sleep(2)
        try:
            since = await _aget("omsguru_down_since")
            self.omsguru_down_since = float(since) if since else None
        except Exception:  # noqa: BLE001
            log.exception("could not read the OMSGuru outage state")
        try:
            await asyncio.to_thread(self._auto_select_warehouses)
        except Exception:  # noqa: BLE001
            log.exception("warehouse auto-select failed")
        while not self._stop:
            self.heartbeat = time.monotonic()
            try:
                ran = await self._tick()
                if not ran:
                    try:
                        await asyncio.wait_for(self._urgent.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive no matter what
                log.exception("sync tick failed: %s", exc)
                await asyncio.sleep(20)

    def _due(self, key: str, every: float) -> bool:
        last = _get_state(key, 0) or 0
        return time.time() - float(last) >= every

    async def _tick(self) -> bool:
        if self._urgent.is_set():
            self._urgent.clear()
            # while OMSGuru is down a "not found" scan does not knock again: the 30-s probe does
            if not self.omsguru_down_since:
                await self._guard("invoices", self.sync_invoices)
                return True
        # Deciding reads the sync state from the database: do it on a worker thread, never on the event loop that
        # serves every station (a blocked loop froze the whole server for 30-60 s in the 3 Oct load test).
        name = await asyncio.to_thread(self._next_job)
        if name is None:
            return False
        await self._guard(name, getattr(self, JOB_METHODS[name]))
        return True

    def _next_job(self) -> str | None:
        """The background job that is due now, if any (runs on a worker thread)."""
        if _get_state("crosscheck_due") and self._warehouse_ids():
            return "crosscheck"  # right after a full refresh, while both are in step
        if self._due("cleanup_last_done", CLEANUP_EVERY_SECONDS):
            return "cleanup"
        if self.omsguru_down_since:
            # OMSGuru is down: knock with the new-AWB sync only (it resumes from its saved position and its success
            # starts the catch-up); every other job keeps its place and waits
            return "invoices" if self._due("invoices_probe_at", DOWN_PROBE_SECONDS) else None
        if self._due("channels_last_done", CHANNELS_EVERY_SECONDS) or not self._has_channels():
            return "channels"
        if self._due("sku_photos_last_done", CHANNELS_EVERY_SECONDS) and self._has_channels():
            return "sku_photos"
        if self._due("invoices_last_done", settings.sync_interval_seconds):
            return "invoices"
        if not self._warehouse_ids():
            return None  # nothing to refresh until a dispatch warehouse is selected (auto or Admin)
        # Open orders first: they are what stations scan, and they load in a few pages.
        if _get_state("open_orders_progress") or self._due("open_orders_last_done", settings.open_orders_refresh_minutes * 60):
            return "open_orders"
        if self._due("cancel_sweep_last_done", settings.cancel_sweep_minutes * 60):
            return "cancel_sweep"
        if ((_get_state("trail_audit_progress") or self._due("trail_audit_last_done", AUDIT_EVERY_SECONDS))
                and self.client.state.estimated_remaining() >= HISTORY_MIN_CREDITS):
            return "audit"
        if (self._due("exit_check_idle_at", EXIT_CHECK_IDLE_SECONDS)
                and self.client.state.estimated_remaining() >= HISTORY_MIN_CREDITS):
            return "exit_check"
        if (settings.history_backfill and _get_state("invoices_cursor") and not _get_state("history_done")
                and self.client.state.estimated_remaining() >= HISTORY_MIN_CREDITS):
            return "history"
        return None

    async def _guard(self, name: str, fn) -> None:
        st = self.jobs[name]
        st.running = True
        st.last_started = time.time()
        job = _Job(st)
        ok, msg = True, ""
        kind = ""  # why it failed: down / key_refused / throttled / error
        cancelled = False
        try:
            msg = await fn(job) or ""
        except asyncio.CancelledError:
            cancelled = True  # stopped mid-run (shutdown / deploy): NOT a success - nothing is recorded
            raise
        except OmsError as exc:
            ok, msg = False, str(exc)
            kind = ("key_refused" if isinstance(exc, OmsAuthError) else "down" if isinstance(exc, OmsDownError)
                    else "throttled" if isinstance(exc, OmsThrottled) else "error")
            log.warning("%s failed: %s", name, exc)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
            log.exception("%s crashed", name)
        finally:
            if cancelled:
                st.running = False
        if cancelled:
            return
        recovered = st.last_ok is False and ok
        st.running = False
        st.last_finished = time.time()
        st.last_ok = ok
        st.last_message = msg[:300]
        st.records = job.records
        # No-op polls are neither logged nor announced: every "sync" event makes every open page reload.
        if job.records or not ok or recovered:
            await asyncio.to_thread(_log_run, name, ok, job.calls, job.records, msg)
            hub.publish("sync", {"job": name, "ok": ok, "message": msg[:200], "records": job.records})
        if name == "invoices":
            try:
                if self.omsguru_down_since:
                    await _aset("invoices_probe_at", time.time())
                await self._note_omsguru(ok, kind, msg)
            except Exception:  # noqa: BLE001 - bookkeeping must never stop the sync loop
                log.exception("could not record the OMSGuru connection state")
        if not ok:
            # Don't hot-loop a failing job (longer while OMSGuru is down).
            await asyncio.sleep(DOWN_PROBE_SECONDS if self.omsguru_down_since else 15)

    async def _record_omsguru(self, ok: bool, kind: str, msg: str) -> None:
        """Connection status (Admin -> OMSGuru connection, banners, /api/health) and the incident history: one entry
        per stretch of "down" or "key refused", with start, end and OMSGuru's last error."""
        now = time.time()
        st = await _aget("omsguru_status") or {}
        state = "ok" if ok else kind
        if state != st.get("state"):
            inc = await _aget("omsguru_incidents") or []
            if inc and inc[-1].get("end") is None:
                inc[-1]["end"] = now
            if state in OUTAGE_KINDS:
                inc.append({"kind": state, "start": now, "end": None, "error": msg[:200]})
                log.warning("OMSGuru connection: %s - %s", state, msg)
            elif st.get("state") in OUTAGE_KINDS:
                log.warning("OMSGuru connection: working again")
            await _aset("omsguru_incidents", inc[-INCIDENTS_KEPT:])
            st = {"state": state, "since": now, "last_ok": st.get("last_ok")}
        st["checked_at"] = now
        if ok:
            st["last_ok"], st["last_error"] = now, ""
        else:
            st["last_error"] = msg[:300]
        await _aset("omsguru_status", st)

    async def _note_omsguru(self, ok: bool, kind: str = "down", msg: str = "") -> None:
        """The new-AWB sync is the pulse of the OMSGuru connection. Remember (in the database, so a restart does not
        forget) when it started failing; when it works again after CATCH_UP_AFTER_SECONDS or more, catch up in full.

        Nothing is skipped even without this: the new-AWB cursor only moves after a window was read completely, so the
        first successful run reads from where it stopped up to now; the open-order refresh and the order-trail audit
        keep their page and continue. The catch-up adds a deep check on top: every invoice of the last days AWB by AWB
        (missing ones added), the whole Packed / Ready-to-ship list and the cancellations, all at once."""
        if not ok and kind not in OUTAGE_KINDS:
            return  # throttled / a rejected request: OMSGuru is there - not an outage
        await self._record_omsguru(ok, kind, msg)
        since = await _aget("omsguru_down_since")
        if not ok:
            if not since:
                since = time.time()
                await _aset("omsguru_down_since", since)
                log.warning("OMSGuru is not answering - outage mode: scans use the stored orders, the sync probes "
                            "every %ss and catches up when it is back", DOWN_PROBE_SECONDS)
            self.omsguru_down_since = float(since)
            return
        self.omsguru_down_since = None
        if not since:
            return
        await _aset("omsguru_down_since", None)
        down_for = time.time() - float(since)
        if down_for < CATCH_UP_AFTER_SECONDS:
            return
        await _aset("trail_audit_progress", None)
        await _aset("trail_audit_force_deep", True)
        await _aset("trail_audit_last_done", 0)
        await asyncio.to_thread(self.request_full, "open_orders")
        await asyncio.to_thread(self.request_full, "cancel_sweep")
        msg = (f"OMSGuru answering again after {int(down_for // 60)} min - catch-up started: new AWBs resumed from where "
               "they stopped; every order of the last 7 days re-checked; Packed / Ready-to-ship list and cancellations "
               "refreshed")
        log.warning(msg)
        await asyncio.to_thread(_log_run, "catch_up", True, 0, 0, msg)
        hub.publish("sync", {"job": "catch_up", "ok": True, "message": msg[:200], "records": 0})

    # ---- scan-time live lookup ---------------------------------------------------------------

    def live_lookup(self, raw: str, norm: str, orders: list[OmsOrder]) -> dict[str, Any]:
        """Fetch the scanned shipment from OMSGuru right now. Runs in the scan request's thread.

        OMSGuru cannot search by AWB (verified against the live API), so:
          1. AWB known locally -> order_details by sub-order id or order id, whichever works for the
             channel (see _lookup_plan): fresh status, items, buyer, courier.
          2. AWB unknown -> pull the newest invoices now (an AWB is printed when the invoice is made).
          3. Still unknown -> the barcode may be an order / sub-order id: order_details with it.
        Never waits for API credits and stops after LIVE_TIMEOUT_SECONDS - the scan then uses the
        local copy, so scanning never stalls.
        """
        loop = hub._loop  # noqa: SLF001 - the app's event loop, where the HTTP client lives
        info: dict[str, Any] = {"live": "off", "calls": 0, "changed": False}
        if not settings.live_lookup or loop is None:
            return info
        if self.omsguru_down_since:
            info["live"] = "down"  # OMSGuru is down: answer from the stored orders at once, no waiting on errors
            return info
        t0 = time.monotonic()
        deadline = t0 + settings.live_timeout_seconds

        def call(coro):
            left = deadline - time.monotonic()
            if left <= 0.05:
                coro.close()
                raise TimeoutError
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                result = fut.result(timeout=left)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                raise TimeoutError from None
            info["calls"] += 1
            return result

        try:
            if orders:
                shipments = {o.tracking_norm or f"#{o.id}": o for o in orders}
                got_awbs: set[str] = set()
                found = False
                for o in list(shipments.values())[:2]:
                    for method, kwargs in self._lookup_plan(o):
                        if info["calls"] >= 3:
                            break
                        rows, exact = self._judge(o, method, call(self.client.order_details(live=True, **kwargs)))
                        if rows:
                            self._apply_rows(rows, "live")
                            info["changed"] = True
                            found = True
                            got_awbs |= {normalize_tracking(r.get("shipment_tracker")) for r in rows}
                        if exact:
                            break
                wanted = {o.tracking_norm for o in orders if o.tracking_norm}
                info["live"] = "fresh" if wanted & got_awbs or (found and not wanted) else "unconfirmed"
            else:
                self._pull_recent_invoices(call, info, keep_awb=norm)
                if info["changed"] and self._known(norm):
                    info["live"] = "fresh"
                else:
                    info["live"] = "not_in_oms"
                    # The barcode may be an order id / sub-order id scanned instead of the AWB.
                    for kwargs in ({"order_id": raw}, {"sub_order_id": raw}):
                        got = call(self.client.order_details(live=True, **kwargs))
                        rows = [r for r in got if _row_matches(r, sub=kwargs.get("sub_order_id", ""),
                                                               order_id=kwargs.get("order_id", ""), awb="")]
                        if rows:
                            self._apply_rows(rows, "live")
                            info["changed"] = True
                            info["live"] = "fresh" if self._known(norm) else "not_in_oms"
                            break
        except OmsBusy:
            info["live"] = "busy"
        except TimeoutError:
            info["live"] = "timeout"
        except OmsError as exc:
            info["live"] = "error"
            info["error"] = str(exc)[:200]
        self.live_stats[info["live"]] += 1
        info["ms"] = int((time.monotonic() - t0) * 1000)
        return info

    def _pull_recent_invoices(self, call, info: dict[str, Any], keep_awb: str = "") -> None:
        """Invoices created since the last background sync (at most 3 pages). Shared between stations:
        if another scan pulled in the last few seconds, its result is already in the local copy."""
        with self._live_pull_lock:
            now = time.time()
            if now - self._last_live_pull < 4:
                info["changed"] = True  # re-read the local copy; another station just refreshed it
                return
            self._last_live_pull = now
        cursor = float(_get_state("invoices_cursor") or (now - 3600))
        start, end, last_id = int(min(cursor, now) - 120), int(now), 0
        for _ in range(3):
            rows = call(self.client.list_invoices(start, end, last_id=last_id, limit=100, live=True))
            if rows:
                self._apply_rows(rows, "invoices", keep_awbs={keep_awb} if keep_awb else None)
                info["changed"] = True
            ids = [int(r.get("last_id") or 0) for r in rows if r.get("last_id") not in (None, "")]
            if len(rows) < 100 or not ids or max(ids) <= last_id:
                break
            last_id = max(ids)

    def _known(self, norm: str) -> bool:
        with session_scope() as db:
            return bool(find_orders(db, norm))

    def _lookup_plan(self, o: OmsOrder) -> list[tuple[str, dict[str, str]]]:
        """The order_details calls that can find one stored shipment, best first.

        OMSGuru answers differently per channel (live API, 2 Oct 2026): by sub-order id works for Myntra and
        Cocoblu; for Flipkart and Meesho it returns ~50 unrelated rows, for Ajio "No order found". Those only
        work by order id - which returns ONE shipment, so an order split into several shipments may come back
        with the wrong one. Try what last worked for the channel first; drop what never worked for it."""
        sub = next((x for x in (o.sub_order_ids or "").split(",") if x), "")
        plan = ([("sub", {"sub_order_id": sub})] if sub else []) \
            + ([("order", {"order_id": o.channel_order_id})] if o.channel_order_id else [])
        st, ch = self.lookup_stats, o.channel_id
        useful = [p for p in plan if st[(ch, p[0], "hit")] or st[(ch, p[0], "miss")] < LOOKUP_GIVE_UP] or plan
        best = self._lookup_pref.get(ch)
        return sorted(useful, key=lambda p: p[0] != best)

    def _judge(self, o: OmsOrder, method: str, got: list[dict]) -> tuple[list[dict], bool]:
        """Rows of this order from an order_details answer, and whether this exact shipment is among them."""
        sub = next((x for x in (o.sub_order_ids or "").split(",") if x), "")
        rows = [r for r in got if _row_matches(r, sub=sub, order_id=o.channel_order_id, awb=o.tracking_norm)]
        exact = any(_is_shipment(r, o) for r in rows)
        self.lookup_stats[(o.channel_id, method, "hit" if exact else "miss")] += 1
        if exact:
            self._lookup_pref[o.channel_id] = method
        return rows, exact

    # ---- helpers -----------------------------------------------------------------------------

    def _has_channels(self) -> bool:
        with session_scope() as db:
            return (db.scalar(select(func.count(Channel.id))) or 0) > 0

    def _warehouse_ids(self) -> list[int]:
        with session_scope() as db:
            return [w.id for w in db.scalars(select(Warehouse).where(Warehouse.sync_enabled.is_(True)).order_by(Warehouse.id))]

    def _auto_select_warehouses(self) -> str | None:
        """Sync only the warehouses your invoices actually ship from. Admin edits switch this off."""
        if _get_state("warehouses_mode") == "manual":
            return None
        with session_scope() as db:
            counts = db.execute(
                select(OmsOrder.warehouse, func.count(OmsOrder.id)).where(OmsOrder.warehouse != "").group_by(OmsOrder.warehouse)
            ).all()
            total = sum(n for _, n in counts)
            whs = list(db.scalars(select(Warehouse)))
            if not whs:
                return None
            by_code = {(code or "").strip().lower(): n for code, n in counts}
            share = {
                w.id: max(by_code.get((w.name or "").strip().lower(), 0), by_code.get((w.alias or "").strip().lower(), 0)) / total
                if total else 0.0
                for w in whs
            }
            decided = total >= 5
            chosen = [w for w in whs if share[w.id] >= WAREHOUSE_MIN_SHARE] if decided else []
            if not chosen:
                # Too little data (or codes don't match names): fall back to the only warehouse, or
                # OMSGuru's own "DEFAULT <client id>" warehouse, and look again after the next sync.
                decided = False
                chosen = whs if len(whs) == 1 else [w for w in whs if (w.name or "").strip().lower().startswith("default")]
                if not chosen:
                    return None  # leave it to the admin
            for w in whs:
                w.sync_enabled = w in chosen
            names = ", ".join(f"{w.name or w.id}" + (f" ({share[w.id]:.0%})" if decided else "") for w in chosen)
        if decided:
            if _get_state("warehouses_mode") != "auto":
                log.info("Auto-selected dispatch warehouse(s) for sync: %s", names)
            _set_state("warehouses_mode", "auto")
        return names

    def _apply_rows(self, rows: list[dict], source: str, keep_awbs: set[str] | None = None) -> tuple[int, set[str]]:
        """_apply_rows_once, once more if another writer inserted the same new order / SKU photo at the same moment
        (a scan's live lookup and the background sync, or two packets of one new SKU): the retry updates their row
        instead of failing the whole page (a scan would otherwise show "OMSGuru did not answer")."""
        from sqlalchemy.exc import IntegrityError, OperationalError

        try:
            return self._apply_rows_once(rows, source, keep_awbs)
        except IntegrityError:
            log.info("apply %s: another writer stored the same rows first - applying again", source)
        except OperationalError as exc:  # PostgreSQL deadlock between two batches touching the same orders
            if "deadlock" not in str(exc).lower():
                raise
            log.info("apply %s: deadlock with another batch - applying again", source)
        return self._apply_rows_once(rows, source, keep_awbs)

    def _apply_rows_once(self, rows: list[dict], source: str, keep_awbs: set[str] | None = None) -> tuple[int, set[str]]:
        """Upsert API rows into the working set. Returns (rows stored or updated, AWBs touched).

        Stored as new rows: Packed / Ready-to-ship orders; any invoice that carries an AWB generated in the
        last RETAIN_ORDERS_DAYS (whatever its status now - needed for "generated vs scanned"); anything fetched
        live for a scan. Rows without an AWB (e.g. cancelled before processing) only UPDATE stored orders.
        """
        if not rows:
            return 0, set()
        keep_awbs = keep_awbs or set()
        touched: set[str] = set()
        channels: set[int | None] = set()
        stored = 0
        now = utcnow()
        retain_from = now - timedelta(days=settings.retain_orders_days)
        with session_scope() as db:
            matcher = ChannelMatcher(db.scalars(select(Channel)))
            mine = _dispatch_warehouses(db)
            sku_set: set[str] = set()
            for row in rows:
                itms = row.get("order_items") or row.get("items") or []
                if isinstance(itms, dict):
                    itms = list(itms.values())
                for it in itms:
                    if isinstance(it, dict):
                        sk = normalize_sku_key(it.get("sku_code") or it.get("sku") or it.get("product_sku") or it.get("sku_name"))
                        if sk:
                            sku_set.add(sk)
            photo_cache: dict[str, tuple[str, str]] = {}
            if sku_set:
                p_rows = db.scalars(select(SkuPhoto).where(SkuPhoto.sku.in_(list(sku_set)))).all()
                photo_cache = {p.sku: (p.image_url, p.title) for p in p_rows}

            for row in rows:
                data = parse_order_row(row, source)
                try:
                    c_items = json.loads(data["items_json"])
                    items_modified = False
                    for ci in c_items:
                        sk = normalize_sku_key(ci.get("sku"))
                        if not sk:
                            continue
                        if not ci.get("image_url") and sk in photo_cache:
                            ci["image_url"] = photo_cache[sk][0]
                            if not ci.get("title") and photo_cache[sk][1]:
                                ci["title"] = photo_cache[sk][1]
                            items_modified = True
                        elif ci.get("image_url"):
                            if sk not in photo_cache:
                                photo_cache[sk] = (ci["image_url"], ci.get("title") or "")
                                db.add(SkuPhoto(sku=sk, title=ci.get("title") or "", image_url=ci["image_url"],
                                                marketplace=data.get("channel_label") or "", updated_at=now))
                    if items_modified:
                        data["items_json"] = json.dumps(c_items, ensure_ascii=False)
                except Exception:
                    pass
                data["channel_id"] = matcher.match(data["channel_label"], data["company"])
                existing = _find_existing(db, data)
                awb_at = data.get("invoice_date")
                recent_awb = bool(data["tracking_norm"]) and (awb_at is None or awb_at >= retain_from)
                may_insert = (source in ("orders", "live") or data["status_group"] in WORKING_SET
                              or (data["tracking_norm"] and data["tracking_norm"] in keep_awbs)
                              or (source == "invoices" and recent_awb))
                if existing is None and not may_insert:
                    continue  # not dispatchable and not ours to track - don't store it
                wh = str(row.get("warehouse") or "").strip().lower()
                if existing is None and source == "invoices" and mine and wh and wh not in mine:
                    continue  # Amazon FBA / marketplace fulfilment centre: ships itself, never pending here
                stored += 1
                if existing is None:
                    existing = OmsOrder(first_seen_at=now)
                    db.add(existing)
                    if data["tracking_norm"]:
                        _retire_replaced_awbs(db, data, now)
                else:
                    # Never wipe a known AWB with a blank one from a later status row.
                    for keep in ("tracking_raw", "tracking_norm", "shipping_company", "invoice_id", "oms_key"):
                        if not data.get(keep) and getattr(existing, keep):
                            data.pop(keep, None)
                    if existing.oms_key != data.get("oms_key", existing.oms_key):
                        clash = db.scalar(select(OmsOrder.id).where(OmsOrder.oms_key == data["oms_key"]))
                        if clash:
                            data.pop("oms_key")
                    # A replaced AWB stays retired when an old invoice row still carries it - unless OMSGuru shows it
                    # open (Packed / Ready-to-ship) again, i.e. current, or cancelled / returned.
                    if existing.status_group == "REPLACED" and                             data["status_group"] not in WORKING_SET + ("CANCELLED", "RETURN"):
                        data["status_group"], data["status_text"] = existing.status_group, existing.status_text
                for k, v in data.items():
                    setattr(existing, k, _fit(k, v))
                existing.synced_at = now
                if existing.tracking_norm and existing.awb_generated_at is None:
                    existing.awb_generated_at = existing.invoice_date or now
                if data["status_group"] in WORKING_SET:
                    existing.seen_open_at = now
                    existing.left_at = None
                elif existing.left_at is None:
                    existing.left_at = now  # no longer Packed / Ready-to-ship: stop syncing, keep last state
                db.flush()
                channels.add(existing.channel_id)
                if existing.tracking_norm:
                    touched.add(existing.tracking_norm)
        if touched:
            with session_scope() as db:
                reverify_scans(db, touched)
        for cid in channels:  # pending / queue numbers of these channels changed
            cache.invalidate(cid)
        return stored, touched

    # ---- jobs --------------------------------------------------------------------------------

    async def refresh_channels(self, job: _Job) -> str:
        chans = await self.client.list_channels()
        job.calls += 1
        whs: list[dict] = []
        need_wh = not await asyncio.to_thread(self._warehouse_ids)
        if need_wh:
            whs = await self.client.list_warehouses()
            job.calls += 1
        job.records = await asyncio.to_thread(_store_channels, chans, whs)
        await _aset("channels_last_done", time.time())
        if need_wh and not whs:
            return f"{len(chans)} channels; no warehouses returned - set them in Admin"
        return f"{len(chans)} channels, {len(whs)} warehouses"

    async def refresh_sku_photos(self, job: _Job) -> str:
        """Fetch listing photos from active channels to populate SKU product images."""
        with session_scope() as db:
            active_channels = list(db.scalars(select(Channel).where(Channel.scan_enabled.is_(True), Channel.oms_status == "active")))
        if not active_channels:
            await _aset("sku_photos_last_done", time.time())
            return "No active channels to sweep"
        total_photos = 0
        for ch in active_channels:
            last_id = 0
            for _ in range(10):  # up to 10 pages per channel
                try:
                    listings = await self.client.list_channel_listings(ch.id, last_id=last_id, limit=100)
                except Exception as exc:
                    log.warning("list_channel_listings failed for channel %s: %s", ch.name, exc)
                    break
                job.calls += 1
                if not listings:
                    break
                stored = await asyncio.to_thread(_store_sku_photos, listings, ch.marketplace or ch.name)
                total_photos += stored
                ids = [int(l.get("id") or 0) for l in listings if l.get("id") not in (None, "")]
                if len(listings) < 100 or not ids or max(ids) <= last_id:
                    break
                last_id = max(ids)
        job.records = total_photos
        await _aset("sku_photos_last_done", time.time())
        return f"Synced {total_photos} SKU photos across {len(active_channels)} channels"

    async def sync_invoices(self, job: _Job) -> str:
        now = int(time.time())
        cursor = await _aget("invoices_cursor")  # unix ts fully synced up to
        window = await _aget("invoices_window")  # in-flight window {start, end, last_id}
        if not window:
            if cursor:
                start = int(cursor) - INVOICE_OVERLAP_SECONDS
            else:  # first run: everything generated since the start of today's dispatch day
                start = int(day_bounds_utc(today_dispatch_date())[0].replace(tzinfo=timezone.utc).timestamp())
            window = {"start": start, "end": now, "last_id": 0}
        total = 0
        while True:
            rows = await self.client.list_invoices(window["start"], window["end"], last_id=window["last_id"])
            job.calls += 1
            n, _ = await asyncio.to_thread(self._apply_rows, rows, "invoices")
            total += n
            job.records = total
            last_ids = [int(r.get("last_id") or 0) for r in rows if r.get("last_id") not in (None, "")]
            if len(rows) < settings.batch_limit or not last_ids or max(last_ids) <= window["last_id"]:
                break
            window["last_id"] = max(last_ids)
            await _aset("invoices_window", window)
            if self._urgent.is_set():
                self._urgent.clear()
        await _aset("invoices_cursor", window["end"])
        await _aset("invoices_window", None)
        await _aset("invoices_last_done", time.time())
        picked = await asyncio.to_thread(self._auto_select_warehouses)
        return f"{total} invoice rows" + (f"; syncing warehouse {picked}" if picked and total else "")

    async def step_open_orders(self, job: _Job) -> str:
        """Fetch ONE page of the open-order refresh so urgent invoice syncs can interleave."""
        whs = await asyncio.to_thread(self._warehouse_ids)
        if not whs:
            await _aset("open_orders_last_done", time.time())
            return "No warehouses configured"
        prog = await _aget("open_orders_progress")
        now = int(time.time())
        if not prog:
            prog = {"run_started": now, "wh": 0, "st": 0, "last_id": 0, "pages": 0, "rows": 0,
                    "start": now - settings.order_lookback_days * 86400, "end": now}
        wh_id = whs[min(prog["wh"], len(whs) - 1)]
        status_id = OPEN_STATUSES[prog["st"]]
        rows = await self.client.list_orders(prog["start"], prog["end"], wh_id, status_id, last_id=prog["last_id"])
        job.calls += 1
        n, _ = await asyncio.to_thread(self._apply_rows, rows, "orders")
        job.records = n
        prog["pages"] += 1
        prog["rows"] += n
        last_ids = [int(r.get("last_id") or 0) for r in rows if r.get("last_id") not in (None, "")]
        if len(rows) >= settings.batch_limit and last_ids and max(last_ids) > prog["last_id"]:
            prog["last_id"] = max(last_ids)
            await _aset("open_orders_progress", prog)
            return f"warehouse {wh_id} status {status_id}: page {prog['pages']}"
        # advance to next status / warehouse
        prog["last_id"] = 0
        prog["st"] += 1
        if prog["st"] >= len(OPEN_STATUSES):
            prog["st"] = 0
            prog["wh"] += 1
        if prog["wh"] < len(whs):
            await _aset("open_orders_progress", prog)
            return f"warehouse {wh_id} status {status_id} done"
        moved = await asyncio.to_thread(self._mark_moved, prog)
        await _aset("open_orders_progress", None)
        await _aset("open_orders_last_done", time.time())
        await _aset("crosscheck_due", True)
        return f"Open-order refresh complete: {prog['rows']} rows in {prog['pages']} pages, {moved} left RTS"

    def _mark_moved(self, prog: dict) -> int:
        """Orders that were OPEN but did not appear in a complete Packed + Ready-to-ship refresh have left
        the working set (shipped, cancelled, ...). They are kept briefly so a late scan is still caught."""
        from ..timeutil import from_unix

        started = from_unix(prog["run_started"])
        with session_scope() as db:
            res = db.execute(
                update(OmsOrder)
                .where(
                    OmsOrder.status_group == "OPEN",
                    (OmsOrder.seen_open_at.is_(None)) | (OmsOrder.seen_open_at < started),
                )
                .values(status_group="MOVED", left_at=utcnow())
            )
            return res.rowcount or 0

    async def crosscheck(self, job: _Job) -> str:
        """OMSGuru's own pending report (order_aging) vs the local copy: Packed + Ready-to-ship orders per sales
        channel, inside the ORDER_LOOKBACK_DAYS window the refresh covers. Runs after every full refresh, when
        both should agree. A mismatch schedules one early re-refresh (usually orders that moved mid-refresh);
        if it is still there after that, Admin shows it."""
        await _aset("crosscheck_due", False)
        refreshed_at = await _aget("open_orders_last_done")
        lookback = settings.order_lookback_days
        oms_all: Counter[int] = Counter()
        oms_old: Counter[int] = Counter()
        names: dict[int, str] = {}
        for wh in await asyncio.to_thread(self._warehouse_ids):
            for days_ago, bucket in ((0, oms_all), (lookback, oms_old)):
                data = await self.client.order_aging(wh, days_ago)
                job.calls += 1
                for ch in data.get("by_channel") or []:
                    cid = int(ch.get("channel_company_id") or 0)
                    names.setdefault(cid, str(ch.get("channel_company_name") or ""))
                    bucket[cid] += sum(int((v or {}).get("orders") or 0) for k, v in (ch.get("by_status") or {}).items()
                                       if str(k).strip().lower() in AGING_OPEN)
        res = await asyncio.to_thread(_compare_with_local, oms_all, oms_old, names)
        res.update(checked_at=time.time(), refreshed_at=refreshed_at, lookback_days=lookback)
        prev = await _aget("crosscheck") or {}
        res["retry"] = not res["ok"] and prev.get("ok", True)
        if res["retry"]:
            await _aset("open_orders_last_done", time.time() - settings.open_orders_refresh_minutes * 60 + 300)
        await _aset("crosscheck", res)
        job.records = res["oms"]
        if res["ok"]:
            return f"Matches OMSGuru: {res['oms']} Packed / Ready-to-ship in {len(res['channels'])} channels"
        off = [f"{c['name']} {c['local']} here vs {c['oms']} in OMSGuru" for c in res["channels"] if not c["ok"]]
        if res["unmapped_local"]:
            off.append(f"{res['unmapped_local']} orders of unmapped channels")
        return ("Differs from OMSGuru: " + "; ".join(off) + (" - refreshing again in 5 min" if res["retry"] else ""))[:300]

    async def exit_check(self, job: _Job) -> str:
        """Orders that left Packed / Ready-to-ship without being scanned: ask OMSGuru what they are now, so
        "Left RTS, not scanned" holds real misses (shipped / in transit) and cancellations land in "Cancelled
        after AWB" - also those older than CANCEL_CHECK_DAYS. Spare API credits only, a few per run."""
        checked, statuses = 0, Counter()
        while checked < EXIT_CHECK_BATCH and not self._urgent.is_set() \
                and self.client.state.estimated_remaining() >= HISTORY_MIN_CREDITS + 1:
            o = await asyncio.to_thread(_next_exit_candidate)
            if o is None:
                break
            rows: list[dict] = []
            try:
                for method, kwargs in self._lookup_plan(o):
                    got = await self.client.order_details(min_credits=HISTORY_MIN_CREDITS, **kwargs)
                    job.calls += 1
                    mine, exact = self._judge(o, method, got)
                    rows += mine
                    if exact:
                        break
            except OmsThrottled:
                raise
            except OmsError as exc:  # e.g. an id OMSGuru rejects - don't retry the same order forever
                log.info("exit check of %s failed: %s", o.tracking_raw, exc)
            await asyncio.to_thread(self._apply_rows, rows, "live")
            statuses[await asyncio.to_thread(_mark_exit_checked, o.id)] += 1
            checked += 1
        job.records = checked
        if not checked:
            await _aset("exit_check_idle_at", time.time())
            return "nothing to check"
        return f"{checked} orders that left RTS unscanned: " + ", ".join(f"{n} {s}" for s, n in statuses.most_common())

    async def cleanup(self, job: _Job) -> str:
        n_orders = await asyncio.to_thread(prune_orders)
        n_scans = await asyncio.to_thread(prune_scans)
        await asyncio.to_thread(prune_sync_logs)
        await asyncio.to_thread(optimize)  # keep the query planner's statistics current
        await _aset("cleanup_last_done", time.time())
        job.records = n_orders + n_scans
        parts = []
        if n_orders:
            parts.append(f"removed {n_orders} orders older than {settings.retain_orders_days} days")
        if n_scans:
            parts.append(f"removed {n_scans} scans older than {settings.scan_retention_days} days")
        return "; ".join(parts) or "nothing to remove"

    async def step_history(self, job: _Job) -> str:
        """One page of the one-time backfill of earlier days' AWBs (newest day first), on spare credits only."""
        st = await _aget("history_backfill")
        if not st:
            today = today_dispatch_date()
            counted_from = await asyncio.to_thread(tracking.start_date)  # never fill days before the admin's start
            days = [d for d in (today - timedelta(days=i) for i in range(1, settings.retain_orders_days))
                    if counted_from is None or d >= counted_from]
            wins = []
            for d in days:
                a, b = day_bounds_utc(d)
                wins.append([int(a.replace(tzinfo=timezone.utc).timestamp()), int(b.replace(tzinfo=timezone.utc).timestamp()) - 1,
                             d.isoformat()])
            st = {"windows": wins, "last_id": 0, "rows": 0, "pages": 0}
        if not st["windows"]:
            await _aset("history_backfill", None)
            await _aset("history_done", True)
            return "history complete"
        start, end, label = st["windows"][0]
        rows = await self.client.list_invoices(start, end, last_id=st["last_id"], min_credits=HISTORY_MIN_CREDITS)
        job.calls += 1
        n, _ = await asyncio.to_thread(self._apply_rows, rows, "invoices")
        job.records = n
        st["rows"] += n
        st["pages"] += 1
        ids = [int(r.get("last_id") or 0) for r in rows if r.get("last_id") not in (None, "")]
        if len(rows) >= settings.batch_limit and ids and max(ids) > st["last_id"]:
            st["last_id"] = max(ids)
        else:
            st["windows"].pop(0)
            st["last_id"] = 0
        await _aset("history_backfill", st)
        return f"{label}: {n} AWBs (page {st['pages']})"

    async def step_audit(self, job: _Job) -> str:
        """Order-trail check, one page per tick on spare credits: re-read EVERY invoice of today's and yesterday's
        dispatch day from OMSGuru and compare it AWB by AWB with the local copy. An AWB the incremental sync missed
        (e.g. the AWB came after the invoice and the order left Ready-to-ship before the open-order refresh saw it)
        is added, statuses are refreshed (cancellations leave pending), and the result per day and channel -
        "OMSGuru 3,000 AWBs, 3,000 here" - is kept for Admin and the Pending page."""
        st = await _aget("trail_audit_progress")
        if not st:
            today = today_dispatch_date()
            counted_from = await asyncio.to_thread(tracking.start_date)
            from ..timeutil import to_local

            # after a database restore (restore_backup.py) the deep round runs at once, whatever the hour
            deep = bool(await _aget("trail_audit_force_deep")) or (
                await _aget("trail_audit_deep_on") != today.isoformat() and to_local(utcnow()).hour >= AUDIT_DEEP_HOUR)
            n_days = min(AUDIT_DEEP_DAYS, settings.retain_orders_days) if deep else AUDIT_DAYS
            wins = []
            for d in (today - timedelta(days=i) for i in range(n_days)):
                if counted_from is not None and d < counted_from:
                    continue
                a, b = day_bounds_utc(d)
                wins.append([int(a.replace(tzinfo=timezone.utc).timestamp()),
                             int(b.replace(tzinfo=timezone.utc).timestamp()) - 1, d.isoformat()])
            st = {"windows": wins, "last_id": 0, "pages": 0, "tally": {}, "started": time.time(),
                  "deep": deep, "day": today.isoformat()}
        if not st["windows"]:
            await _aset("trail_audit_progress", None)
            await _aset("trail_audit_last_done", time.time())
            if st.get("deep"):
                await _aset("trail_audit_deep_on", st.get("day"))
                await _aset("trail_audit_force_deep", None)
            return "nothing to check (before the tracking start date)"
        start, end, label = st["windows"][0]
        rows = await self.client.list_invoices(start, min(end, int(time.time())), last_id=st["last_id"],
                                               min_credits=HISTORY_MIN_CREDITS)
        job.calls += 1
        oms, here, added = await asyncio.to_thread(self._audit_page, rows)
        day = st["tally"].setdefault(label, {})
        for cid in set(oms) | set(here):
            c = day.setdefault(cid, {"oms": 0, "app": 0, "added": 0})
            c["oms"] += oms[cid]
            c["app"] += here[cid]
            c["added"] += added[cid]
        job.records = sum(added.values())
        st["pages"] += 1
        ids = [int(r.get("last_id") or 0) for r in rows if r.get("last_id") not in (None, "")]
        if len(rows) >= settings.batch_limit and ids and max(ids) > st["last_id"]:
            st["last_id"] = max(ids)
            await _aset("trail_audit_progress", st)
            return f"{label}: page {st['pages']}" + (f", {job.records} missing AWBs added" if job.records else "")
        st["windows"].pop(0)
        st["last_id"] = 0
        result = await _aget("trail_audit") or {}
        days = result.get("days") or {}
        chans = day
        days[label] = {
            "checked_at": time.time(),
            "complete": all(c["oms"] == c["app"] for c in chans.values()),
            "oms": sum(c["oms"] for c in chans.values()),
            "app": sum(c["app"] for c in chans.values()),
            "added": sum(c["added"] for c in chans.values()),
            "channels": chans,
        }
        for old in sorted(days)[:-AUDIT_KEEP_DAYS]:
            days.pop(old)
        await _aset("trail_audit", {"days": days, "checked_at": time.time()})
        done = days[label]
        if st["windows"]:
            await _aset("trail_audit_progress", st)
        else:
            await _aset("trail_audit_progress", None)
            await _aset("trail_audit_last_done", time.time())
            if st.get("deep"):
                await _aset("trail_audit_deep_on", st.get("day"))
                await _aset("trail_audit_force_deep", None)
            # Nothing extra either: pending AWBs here (not Packed / Ready-to-ship - those are confirmed by the
            # open-order refresh) that OMSGuru's list did not contain on ANY audited day.
            extra = await asyncio.to_thread(_audit_extras, list(st["tally"]), st["started"])
            result = await _aget("trail_audit") or {"days": {}}
            for d, (n, sample) in extra.items():
                if d in result["days"]:
                    t = result["days"][d]
                    t["extra"], t["extra_awbs"] = n, sample
                    t["complete"] = t["complete"] and n == 0
            await _aset("trail_audit", result)
        cache.clear()
        return (f"{label}: OMSGuru {done['oms']} AWBs, {done['app']} here"
                + (f", {done['added']} missing AWBs added" if done["added"] else "")
                + ("" if done["complete"] else " - NOT all stored, see Admin"))

    def _audit_page(self, rows: list[dict]) -> tuple[Counter, Counter, Counter]:
        """Per sales channel (id as text, "0" = unmapped): AWBs on this page in OMSGuru, in the local copy after
        applying the page, and how many of those were missing before. Only rows of the dispatch warehouse(s) synced
        here count: Amazon FBA / marketplace fulfilment centres (DEL4, BLR8 ...) ship themselves and must never
        turn into "pending" here (7 Oct 2026: 92 FBA invoices in OMSGuru's list, rightly not in the app)."""
        with session_scope() as db:
            mine = _dispatch_warehouses(db)
        if mine:
            rows = [r for r in rows if str(r.get("warehouse") or "").strip().lower() in mine]
        parsed = {}
        for r in rows:
            d = parse_order_row(r, "invoices")
            if d["tracking_norm"]:
                parsed[d["tracking_norm"]] = d
        if not parsed:
            return Counter(), Counter(), Counter()
        with session_scope() as db:
            before = set(db.scalars(select(OmsOrder.tracking_norm).where(OmsOrder.tracking_norm.in_(list(parsed)))))
        self._apply_rows(rows, "invoices")
        with session_scope() as db:
            after = set(db.scalars(select(OmsOrder.tracking_norm).where(OmsOrder.tracking_norm.in_(list(parsed)))))
            db.execute(update(OmsOrder).where(OmsOrder.tracking_norm.in_(list(parsed))).values(audit_seen_at=utcnow()))
            matcher = ChannelMatcher(db.scalars(select(Channel)))
            oms, here, added = Counter(), Counter(), Counter()
            for awb, d in parsed.items():
                cid = str(matcher.match(d["channel_label"], d["company"]) or 0)
                oms[cid] += 1
                if awb in after:
                    here[cid] += 1
                    if awb not in before:
                        added[cid] += 1
        return oms, here, added

    async def cancel_sweep(self, job: _Job) -> str:
        """Find Packed / Ready-to-ship orders that were cancelled. Cancelled orders that never reached the
        working set (cancelled before processing) are ignored - _apply_rows with source "cancel" never inserts."""
        whs = await asyncio.to_thread(self._warehouse_ids)
        now = int(time.time())
        start = now - settings.cancel_check_days * 86400
        total = 0
        for wh_id in whs:
            for status_id in CANCEL_STATUSES:
                last_id = 0
                for _ in range(20):  # hard cap per status
                    rows = await self.client.list_orders(start, now, wh_id, status_id, last_id=last_id)
                    job.calls += 1
                    n, _t = await asyncio.to_thread(self._apply_rows, rows, "cancel")
                    total += n
                    job.records = total
                    ids = [int(r.get("last_id") or 0) for r in rows if r.get("last_id") not in (None, "")]
                    if len(rows) < settings.batch_limit or not ids or max(ids) <= last_id:
                        break
                    last_id = max(ids)
                if self._urgent.is_set():
                    self._urgent.clear()
                    await self._guard("invoices", self.sync_invoices)
        await _aset("cancel_sweep_last_done", time.time())
        return f"{total} working-set orders found cancelled" if total else "no working-set order was cancelled"


def _store_channels(chans: list[dict], whs: list[dict]) -> int:
    """Upsert the sales-channel list (and warehouses on first run); returns the number of channels."""
    with session_scope() as db:
        existing = {c.id: c for c in db.scalars(select(Channel))}
        ordered = sorted(chans, key=lambda x: (str(x.get("status") or "active").lower() not in ("active", "a"), int(x.get("id") or 0)))
        for i, c in enumerate(ordered):
            cid = int(c.get("id") or 0)
            if not cid:
                continue
            row = existing.get(cid)
            status = str(c.get("status") or "active").lower()
            if row is None:
                row = Channel(id=cid, scan_enabled=status in ("active", "a"),
                              color=PALETTE[len(existing)] if len(existing) < len(PALETTE) else NEUTRAL,
                              sort_order=100 + i)
                db.add(row)
                existing[cid] = row
            row.name = str(c.get("name") or f"Channel {cid}")
            row.marketplace = str(c.get("channel_name") or c.get("ledger_name") or row.name)
            row.company = str(c.get("company_name") or "")
            row.oms_status = status
        for w in whs:
            wid = int(w.get("id") or w.get("warehouse_id") or 0)
            if wid and not db.get(Warehouse, wid):
                db.add(Warehouse(id=wid, name=str(w.get("name") or ""), alias=str(w.get("alias") or ""),
                                 sync_enabled=False))
        return len(chans)


def _store_sku_photos(listings: list[dict], marketplace: str) -> int:
    stored = 0
    now = utcnow()
    by_sku: dict[str, tuple[str, str]] = {}
    for l in listings:
        raw_sku = l.get("sku_name") or l.get("sku_code") or l.get("sku") or l.get("product_sku")
        sku_norm = normalize_sku_key(raw_sku)
        if not sku_norm:
            continue
        img = normalize_image_url(l.get("img_url") or l.get("image_url") or l.get("imageUrl") or l.get("image"))
        if not img:
            continue
        title = str(l.get("title") or l.get("name") or "").strip()
        by_sku[sku_norm] = (title, img)

    with session_scope() as db:
        for sku_norm, (title, img) in by_sku.items():
            existing = db.get(SkuPhoto, sku_norm)
            if existing is None:
                db.add(SkuPhoto(sku=sku_norm, title=title, image_url=img, marketplace=marketplace, updated_at=now))
                stored += 1
            else:
                if img != existing.image_url or (title and title != existing.title):
                    existing.image_url = img
                    if title:
                        existing.title = title
                    existing.marketplace = marketplace
                    existing.updated_at = now
                    stored += 1
    return stored


def _dispatch_warehouses(db) -> set[str]:
    """Names / aliases (lower case) of the warehouses synced here; empty = not decided yet (accept all)."""
    return {(v or "").strip().lower() for w in db.scalars(select(Warehouse).where(Warehouse.sync_enabled.is_(True)))
            for v in (w.name, w.alias) if (v or "").strip()}


def _audit_extras(days: list[str], started: float) -> dict[str, tuple[int, list[str]]]:
    """Per audited day (invoice date, like OMSGuru's list): unscanned, still-pending AWBs here that the audit round
    did NOT see in OMSGuru's invoice list - counted here as pending although OMSGuru does not list them."""
    from datetime import date as _date

    from ..timeutil import from_unix

    since = from_unix(int(started))
    settled = since - timedelta(minutes=10)  # invoiced while the round ran: not in the pages already read
    out: dict[str, tuple[int, list[str]]] = {}
    with session_scope() as db:
        mine = _dispatch_warehouses(db)
        for d in days:
            a, b = day_bounds_utc(_date.fromisoformat(d))
            q = (select(OmsOrder.tracking_norm)
                 .where(OmsOrder.tracking_norm != "", OmsOrder.invoice_date >= a, OmsOrder.invoice_date < b,
                        OmsOrder.invoice_date < settled,
                        OmsOrder.status_group.notin_(("OPEN", "CANCELLED", "RETURN", "REPLACED")),
                        ~exists().where(Scan.tracking_norm == OmsOrder.tracking_norm),
                        (OmsOrder.audit_seen_at.is_(None)) | (OmsOrder.audit_seen_at < since)))
            if mine:
                q = q.where(func.lower(OmsOrder.warehouse).in_(mine))
            awbs = sorted(set(db.scalars(q)))
            out[d] = (len(awbs), awbs[:10])
    return out


def _retire_replaced_awbs(db, data: dict, now) -> int:
    """A new AWB for a shipment we already have under another AWB (courier reassigned, label re-made): the old
    AWB is no longer a shipment. If it was never scanned it becomes REPLACED - out of generated / pending, and
    scanning the old label is refused. Same channel + order id + overlapping sub-orders only, so the separate
    shipments of a multi-shipment order (different sub-orders) are never touched."""
    subs = {x for x in (data.get("sub_order_ids") or "").split(",") if x}
    if not data["channel_order_id"] or not subs:
        return 0
    n = 0
    for old in db.scalars(select(OmsOrder).where(
            OmsOrder.channel_order_id == data["channel_order_id"], OmsOrder.channel_label == data["channel_label"],
            OmsOrder.tracking_norm != "", OmsOrder.tracking_norm != data["tracking_norm"],
            OmsOrder.status_group.notin_(("CANCELLED", "RETURN", "REPLACED")))):
        if not subs & {x for x in (old.sub_order_ids or "").split(",") if x}:
            continue
        if db.scalar(select(Scan.id).where(Scan.tracking_norm == old.tracking_norm).limit(1)):
            continue  # it went out under the old label: keep that history
        old.status_group = "REPLACED"
        old.status_text = f"AWB replaced by {data['tracking_norm']}"[:120]
        old.left_at = old.left_at or now
        n += 1
    return n


_LIMITS = {c.name: c.type.length for c in OmsOrder.__table__.columns
           if getattr(c.type, "length", None) and isinstance(c.type.length, int)}


def _fit(column: str, value: Any) -> Any:
    """Cut a text value to its column length: PostgreSQL rejects (and rolls back the whole sync page for) a
    buyer name / warehouse / title longer than the column, where SQLite just stored it."""
    limit = _LIMITS.get(column)
    return value[:limit] if limit and isinstance(value, str) and len(value) > limit else value


def _find_existing(db, data: dict) -> OmsOrder | None:
    """The working-set row this API row describes (same shipment), if any."""
    existing = db.scalar(select(OmsOrder).where(OmsOrder.oms_key == data["oms_key"]))
    if existing is not None:
        return existing
    if data["tracking_norm"]:
        # AWB + order id: one AWB can carry two orders (combined shipment) - keep them as separate rows.
        return db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == data["tracking_norm"],
                                                OmsOrder.channel_order_id == data["channel_order_id"]).limit(1))
    if not data["channel_order_id"]:
        return None
    # Rows without an AWB (typical for cancellations): same order id + channel, and the same sub-order(s)
    # when we know them - an order id alone can cover several shipments.
    cands = list(db.scalars(select(OmsOrder).where(OmsOrder.channel_order_id == data["channel_order_id"],
                                                    OmsOrder.channel_label == data["channel_label"])))
    subs = {x for x in (data.get("sub_order_ids") or "").split(",") if x}
    if subs:
        cands = [c for c in cands if subs & {x for x in (c.sub_order_ids or "").split(",") if x}]
    return cands[0] if len(cands) == 1 else None


def prune_orders() -> int:
    """Keep RETAIN_ORDERS_DAYS of orders (by AWB generation date) for unscanned working-set orders.
    Orders that were SCANNED are preserved for long-term history in PostgreSQL (SCANNED_ORDERS_RETENTION_DAYS,
    default 550 days / ~1.5 years) so all scanned order relations, buyer information, and items remain queryable.
    Orders still Packed / Ready-to-ship are always kept, and so is every other unscanned AWB that still counts as
    pending (not cancelled / returned, AWB on or after the tracking start date)."""
    import json as _json

    from ..models import Scan
    from ..services.scanning import order_payload

    now = utcnow()
    cutoff_working_set = now - timedelta(days=settings.retain_orders_days)
    cutoff_scanned = now - timedelta(days=settings.scanned_orders_retention_days)
    with session_scope() as db:
        # rows from before awb_generated_at existed
        db.execute(update(OmsOrder).where(OmsOrder.awb_generated_at.is_(None), OmsOrder.tracking_norm != "")
                   .values(awb_generated_at=func.coalesce(OmsOrder.invoice_date, OmsOrder.first_seen_at)))

        is_scanned = exists().where(
            (Scan.order_id == OmsOrder.id) | (Scan.tracking_norm == OmsOrder.tracking_norm)
        )

        # Pending until scanned (user, 8 Oct 2026): an unscanned AWB that is not cancelled / returned stays - even
        # when OMS shows it shipped - while it is counted (AWB on or after the admin's "count orders from" date) and
        # at most PENDING_KEEP_DAYS. Only a scan, a cancellation, moving that date forward or that cap lets it go.
        awb_at = func.coalesce(OmsOrder.awb_generated_at, OmsOrder.first_seen_at)
        still_pending = ((OmsOrder.tracking_norm != "") & OmsOrder.status_group.notin_(("CANCELLED", "RETURN"))
                         & (awb_at >= now - timedelta(days=settings.pending_keep_days)))
        counted_from = tracking.start_utc()
        if counted_from is not None:
            still_pending = still_pending & (awb_at >= counted_from)

        unscanned_doomed = (
            OmsOrder.status_group.notin_(WORKING_SET)
            & ~is_scanned
            & ~still_pending
            & (
                (awb_at < cutoff_working_set)
                | ((OmsOrder.tracking_norm == "") & (OmsOrder.synced_at < now - timedelta(days=1)))
            )
        )

        scanned_doomed = (
            OmsOrder.status_group.notin_(WORKING_SET)
            & is_scanned
            & (awb_at < cutoff_scanned)
            & (settings.scanned_orders_retention_days > 0)  # 0 = keep forever
        )

        doomed = list(db.scalars(
            select(OmsOrder).where(unscanned_doomed | scanned_doomed)
        ))
        if not doomed:
            return 0
        ids = [o.id for o in doomed]
        by_id = {o.id: o for o in doomed}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for sc in db.scalars(select(Scan).where(Scan.order_id.in_(chunk))):
                if not sc.order_json:
                    sc.order_json = _json.dumps(order_payload(by_id[sc.order_id]), default=str)
                sc.order_id = None
            db.flush()
            db.execute(OmsOrder.__table__.delete().where(OmsOrder.id.in_(chunk)))
        return len(ids)


def prune_scans() -> int:
    """Long-term scan history: delete scans (and their audit events / empty manifests) older than
    SCAN_RETENTION_DAYS from the scan date. 0 keeps everything forever.

    Deleted PRUNE_BATCH rows at a time, each in its own short transaction: one transaction for a month of
    scans held the write lock for 19 s on 3 years of data, and stations' scans waited the whole time."""
    if settings.scan_retention_days <= 0:
        return 0
    from ..models import Manifest, ScanEvent

    cutoff_day = today_dispatch_date() - timedelta(days=settings.scan_retention_days)
    removed = 0
    for model in (Scan, ScanEvent):
        while True:
            with session_scope() as db:
                ids = list(db.scalars(select(model.id).where(model.dispatch_date < cutoff_day).limit(PRUNE_BATCH)))
                if ids:
                    db.execute(model.__table__.delete().where(model.id.in_(ids)))
            if not ids:
                break
            if model is Scan:
                removed += len(ids)
            time.sleep(0.05)  # let waiting scans write between batches
    with session_scope() as db:
        db.execute(Manifest.__table__.delete().where(
            Manifest.dispatch_date < cutoff_day, ~exists().where(Scan.manifest_id == Manifest.id)))
    return removed


def prune_sync_logs() -> int:
    with session_scope() as db:
        return db.execute(SyncLog.__table__.delete().where(
            SyncLog.started_at < utcnow() - timedelta(days=SYNC_LOG_DAYS))).rowcount or 0


# Backwards-compatible name used by older tests / scripts.
prune_working_set = prune_orders


def _exit_check_filter() -> tuple:
    """Unscanned, still-pending AWBs that left Packed / Ready-to-ship (MOVED, or SHIPPED / UNKNOWN in OMS) and
    OMSGuru has not been asked about since they left, or not for EXIT_RECHECK_HOURS: they stay pending until a
    scan, so a later cancellation / return must still reach them (up to PENDING_KEEP_DAYS)."""
    since = utcnow() - timedelta(days=settings.pending_keep_days)
    counted_from = tracking.start_utc()
    stale = utcnow() - timedelta(hours=EXIT_RECHECK_HOURS)
    return (
        OmsOrder.status_group.in_(("MOVED", "SHIPPED", "UNKNOWN")),
        OmsOrder.tracking_norm != "",
        OmsOrder.awb_generated_at >= (max(since, counted_from) if counted_from else since),
        (OmsOrder.exit_checked_at.is_(None)) | (OmsOrder.exit_checked_at < OmsOrder.left_at)
        | (OmsOrder.exit_checked_at < stale),
        ~exists().where(Scan.tracking_norm == OmsOrder.tracking_norm),
    )


def _next_exit_candidate() -> OmsOrder | None:
    with session_scope() as db:
        # never asked first, then the longest ago
        return db.scalar(select(OmsOrder).where(*_exit_check_filter())
                         .order_by(OmsOrder.exit_checked_at.is_not(None), OmsOrder.exit_checked_at, OmsOrder.left_at,
                                   OmsOrder.id).limit(1))


def _mark_exit_checked(order_id: int) -> str:
    """Remember the lookup; returns the order's status group after it."""
    with session_scope() as db:
        o = db.get(OmsOrder, order_id)
        if o is None:
            return "gone"
        o.exit_checked_at = utcnow()
        return o.status_group if o.status_group != "MOVED" else "not found"


def _compare_with_local(oms_all: Counter[int], oms_old: Counter[int], names: dict[int, str]) -> dict[str, Any]:
    """Per channel: OMSGuru's Packed + Ready-to-ship orders (inside the refresh window) vs the local copy."""
    with session_scope() as db:
        local = dict(db.execute(select(OmsOrder.channel_id, func.count(OmsOrder.id))
                                .where(OmsOrder.status_group == "OPEN").group_by(OmsOrder.channel_id)).all())
        chans = {c.id: c for c in db.scalars(select(Channel))}
    rows = []
    for cid in set(oms_all) | {k for k in local if k}:
        oms, loc = oms_all[cid] - oms_old[cid], local.get(cid, 0)
        if not (oms or loc):
            continue
        c = chans.get(cid)
        rows.append({"channel_id": cid, "name": c.name if c else names.get(cid) or f"Channel {cid}",
                     "color": c.color if c else "", "sort_order": c.sort_order if c else 9999,
                     "oms": oms, "local": loc, "diff": loc - oms, "older": oms_old[cid],
                     # a couple of orders moving while the refresh pages through is normal
                     "ok": abs(loc - oms) <= max(2, oms // 100)})
    rows.sort(key=lambda r: (r["sort_order"], r["name"]))
    unmapped = local.get(None, 0)
    return {"ok": all(r["ok"] for r in rows) and not unmapped, "channels": rows, "unmapped_local": unmapped,
            "oms": sum(r["oms"] for r in rows), "local": sum(r["local"] for r in rows) + unmapped,
            "older_total": sum(oms_old.values())}


def _is_shipment(row: dict, o: OmsOrder) -> bool:
    """The order_details row is this stored shipment: same AWB, or one of its sub-orders."""
    if o.tracking_norm and normalize_tracking(row.get("shipment_tracker")) == o.tracking_norm:
        return True
    subs = {x.strip().upper() for x in (o.sub_order_ids or "").split(",") if x.strip()}
    items = [i for i in row.get("order_items") or [] if isinstance(i, dict)]
    return bool(subs & {str(i.get("channel_sub_order_id") or "").strip().upper() for i in items})


def _row_matches(row: dict, *, sub: str, order_id: str, awb: str) -> bool:
    """True when an order_details row is the shipment we asked for (case-insensitive exact match)."""
    items = row.get("order_items") or []
    subs = {str(i.get("channel_sub_order_id") or "").strip().upper() for i in items if isinstance(i, dict)}
    oids = {str(i.get("channel_order_id") or "").strip().upper() for i in items if isinstance(i, dict)}
    if awb and normalize_tracking(row.get("shipment_tracker")) == awb:
        return True
    if sub and sub.strip().upper() in subs:
        return True
    return bool(order_id) and order_id.strip().upper() in oids


engine_instance: SyncEngine | None = None


def get_engine() -> SyncEngine | None:
    return engine_instance
