"""OMSGuru REST client with a shared, header-aware rate limiter.

OMSGuru allows 60 requests per 5 minutes per client (token bucket, 1 credit refills every 5s).
The same client id may be used by other integrations, so we:
  * run at most one request at a time,
  * read X-OMS-Remaining after every call and keep `OMSGURU_RESERVE_CREDITS` untouched,
  * honour Retry-After on HTTP 429 with exponential back-off.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import random
import ssl
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import truststore

from ..config import settings

log = logging.getLogger("oms.client")

REFILL_SECONDS = 5.0


class OmsError(Exception):
    pass


class OmsThrottled(OmsError):
    pass


class OmsBusy(OmsError):
    """A live (scan-time) call could not run right now; the caller falls back to the local copy."""


LIVE_HTTP_TIMEOUT = 4.0
# How long a scan-time call may wait for another OMSGuru call to finish before the scan uses the local copy.
LIVE_LOCK_WAIT = 0.3


class PriorityLock:
    """asyncio lock where lower priority numbers are served first (0 = scan-time, 1 = background)."""

    def __init__(self) -> None:
        self._locked = False
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()

    async def acquire(self, priority: int = 1) -> None:
        if not self._locked and not self._waiters:
            self._locked = True
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()  # we were handed the lock just as we got cancelled - pass it on
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(True)  # hand over directly; lock stays held
                return
        self._locked = False


@dataclass
class LimiterState:
    limit: int = 60
    remaining: int | None = None
    reset_at: int | None = None
    observed_at: float = 0.0
    calls_total: int = 0
    throttled_total: int = 0
    errors_total: int = 0
    last_error: str = ""
    last_call_at: float = 0.0
    waiting: bool = False
    history: list[tuple[float, str, int]] = field(default_factory=list)  # (ts, path, status)

    def estimated_remaining(self) -> float:
        if self.remaining is None:
            return float(self.limit)
        refilled = (time.time() - self.observed_at) / REFILL_SECONDS
        return min(float(self.limit), self.remaining + refilled)

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "remaining_reported": self.remaining,
            "remaining_estimated": round(self.estimated_remaining(), 1),
            "reserve": settings.oms_reserve_credits,
            "calls_total": self.calls_total,
            "throttled_total": self.throttled_total,
            "errors_total": self.errors_total,
            "last_error": self.last_error,
            "last_call_at": self.last_call_at or None,
            "waiting_for_credit": self.waiting,
            "recent": [{"ts": t, "path": p, "status": s} for t, p, s in self.history[-15:]],
        }


class OmsClient:
    def __init__(self) -> None:
        self.state = LimiterState()
        self._lock = PriorityLock()
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._http = httpx.AsyncClient(
            base_url=settings.oms_base_url,
            timeout=httpx.Timeout(60.0, connect=15.0),
            verify=ctx,
            headers={
                "Authorization": f"Bearer {settings.oms_token}",
                "Oms-Cid": str(settings.oms_client_id),
                "Accept": "application/json",
                "User-Agent": "ForwardScan/1.0",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _record_headers(self, resp: httpx.Response) -> None:
        h = resp.headers
        try:
            if "x-oms-limit" in h:
                self.state.limit = int(h["x-oms-limit"])
            if "x-oms-remaining" in h:
                self.state.remaining = int(h["x-oms-remaining"])
                self.state.observed_at = time.time()
            if "x-oms-reset" in h:
                self.state.reset_at = int(h["x-oms-reset"])
        except ValueError:
            pass

    async def _wait_for_credit(self, headroom: int, min_credits: int = 0) -> None:
        need = max(settings.oms_reserve_credits + headroom + 1, min_credits)
        while True:
            est = self.state.estimated_remaining()
            if est >= need:
                return
            self.state.waiting = True
            await asyncio.sleep(max(1.0, (need - est) * REFILL_SECONDS))
            self.state.waiting = False

    async def request(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        max_attempts: int = 8,
        live: bool = False,
        min_credits: int = 0,
    ) -> Any:
        """Background calls wait for credits and retry; live (scan-time) calls jump the queue,
        never wait for credits and never retry - the scan falls back to the local copy instead."""
        if not settings.oms_token or not settings.oms_client_id:
            raise OmsError("OMSGURU_API_TOKEN / OMSGURU_CLIENT_ID missing in .env")
        if live:
            return await self._live_request(method, path, form=form, params=params)
        attempt = 0
        while True:
            attempt += 1
            # Wait for credits OUTSIDE the lock so scan-time calls are never stuck behind a waiting sync.
            await self._wait_for_credit(settings.live_headroom, min_credits)
            await self._lock.acquire(priority=2 if min_credits else 1)
            retry_in = 0.0
            try:
                if self.state.estimated_remaining() < max(settings.oms_reserve_credits + settings.live_headroom + 1, min_credits):
                    continue  # a live call used the credit meanwhile
                try:
                    resp = await self._send(method, path, form, params, timeout=None)
                except httpx.HTTPError as exc:
                    self.state.errors_total += 1
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
                    if attempt >= max_attempts:
                        raise OmsError(self.state.last_error) from exc
                    retry_in = min(60, 2 ** attempt)
                else:
                    if resp.status_code == 429 or _is_throttle_body(resp):
                        self._mark_throttled()
                        if min_credits or attempt >= max_attempts:
                            # spare-credit work (audit, exit check, history): the next tick decides again -
                            # waiting here would hold up the new-AWB sync behind it
                            raise OmsThrottled("OMSGuru API throttled (rate limit shared with other integrations)")
                        retry_after = _int(resp.headers.get("retry-after"), 5)
                        retry_in = min(120.0, retry_after + REFILL_SECONDS * (2 ** min(attempt - 1, 4)) * random.uniform(0.5, 1.0))
                        log.info("OMSGuru throttled on %s, retrying in %.0fs", path, retry_in)
                    elif resp.status_code >= 500 and attempt < max_attempts:
                        self.state.errors_total += 1
                        self.state.last_error = f"HTTP {resp.status_code} on {path}"
                        retry_in = min(60, 2 ** attempt)
                    else:
                        return self._parse(resp, path)
            finally:
                self._lock.release()
            self.state.waiting = True
            await asyncio.sleep(retry_in)
            self.state.waiting = False

    async def _live_request(self, method: str, path: str, *, form, params) -> Any:
        # Scan-time checks only use credits above what background sync needs, and give up at once when another
        # call is running: at peak, queued live calls used the whole 2.5 s scan budget and starved the invoice
        # sync that brings in new AWBs for every station (simulated load test, 3 Oct 2026).
        floor = settings.oms_reserve_credits + settings.live_headroom + 2
        if self.state.estimated_remaining() < floor:
            raise OmsBusy("No OMSGuru API credit to spare for a live check right now")
        try:
            await asyncio.wait_for(self._lock.acquire(priority=0), LIVE_LOCK_WAIT)
        except asyncio.TimeoutError:
            raise OmsBusy("OMSGuru API busy with another call") from None
        try:
            if self.state.estimated_remaining() < floor:
                raise OmsBusy("No OMSGuru API credit to spare for a live check right now")
            try:
                resp = await self._send(method, path, form, params, timeout=LIVE_HTTP_TIMEOUT)
            except httpx.HTTPError as exc:
                self.state.errors_total += 1
                self.state.last_error = f"{type(exc).__name__}: {exc}"
                raise OmsBusy(self.state.last_error) from exc
            if resp.status_code == 429 or _is_throttle_body(resp):
                self._mark_throttled()
                raise OmsBusy("OMSGuru API throttled")
            return self._parse(resp, path)
        finally:
            self._lock.release()

    async def _send(self, method: str, path: str, form, params, timeout: float | None) -> httpx.Response:
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._http.request(
            method,
            path,
            data={k: v for k, v in (form or {}).items() if v is not None} or None,
            params={k: v for k, v in (params or {}).items() if v is not None} or None,
            **kwargs,
        )
        self.state.calls_total += 1
        self.state.last_call_at = time.time()
        self.state.history.append((time.time(), path, resp.status_code))
        del self.state.history[:-50]
        self._record_headers(resp)
        return resp

    def _mark_throttled(self) -> None:
        self.state.throttled_total += 1
        self.state.remaining = 0
        self.state.observed_at = time.time()

    def _parse(self, resp: httpx.Response, path: str) -> Any:
        if resp.status_code == 401:
            self.state.last_error = ("401 Unauthorized from OMSGuru - the API key was refused (OMSGURU_API_TOKEN / "
                                     "OMSGURU_CLIENT_ID changed?) or OMSGuru itself is having an outage")
            raise OmsError(self.state.last_error)
        if resp.status_code >= 500:
            # their nginx error page is not shown: say what it means
            self.state.errors_total += 1
            self.state.last_error = (f"OMSGuru's server is down (HTTP {resp.status_code}) - an outage on OMSGuru's side, "
                                     "not in this app; the sync retries by itself")
            raise OmsError(self.state.last_error)
        if resp.status_code >= 400:
            self.state.errors_total += 1
            self.state.last_error = f"HTTP {resp.status_code} on {path}: {resp.text[:200]}"
            raise OmsError(self.state.last_error)
        try:
            return resp.json()
        except ValueError as exc:
            self.state.last_error = f"Non-JSON response from {path}"
            raise OmsError(self.state.last_error) from exc

    # ---- endpoints -------------------------------------------------------------------------

    async def list_channels(self) -> list[dict]:
        return _rows(await self.request("GET", "/order_api/list_channels"))

    async def list_warehouses(self) -> list[dict]:
        return _rows(await self.request("GET", "/order_api/warehouses"))

    async def list_channel_listings(self, channel_company_id: int, last_id: int = 0, limit: int | None = None) -> list[dict]:
        form = {
            "channel_company_id": channel_company_id,
            "last_id": last_id,
            "limit": limit or settings.batch_limit,
        }
        return _rows(await self.request("POST", "/order_api/list_channel_listings", form=form))

    async def list_invoices(self, start_ts: int, end_ts: int, last_id: int = 0, limit: int | None = None,
                            live: bool = False, min_credits: int = 0) -> list[dict]:
        form = {
            "start_invoice_date": start_ts,
            "end_invoice_date": end_ts,
            "last_id": last_id,
            "limit": limit or settings.batch_limit,
        }
        return _rows(await self.request("POST", "/order_api/invoices", form=form, live=live, min_credits=min_credits))

    async def list_orders(
        self, start_ts: int, end_ts: int, warehouse_id: int, status_id: int, last_id: int = 0, limit: int | None = None
    ) -> list[dict]:
        form = {
            "start_order_date": start_ts,
            "end_order_date": end_ts,
            "warehouse_id": warehouse_id,
            "status_id": status_id,
            "last_id": last_id,
            "limit": limit or settings.batch_limit,
        }
        return _rows(await self.request("POST", "/order_api/orders", form=form))

    async def order_details(self, *, order_id: str | None = None, sub_order_id: str | None = None,
                            live: bool = False, min_credits: int = 0) -> list[dict]:
        """The only per-order lookup OMSGuru offers (by order id / sub-order id - never by AWB)."""
        form = {"order_id": order_id, "sub_order_id": sub_order_id}
        return _rows(await self.request("POST", "/order_api/order_details", form=form, live=live,
                                        min_credits=min_credits))

    async def order_aging(self, warehouse_id: int, days_ago: int = 0) -> dict:
        """OMSGuru's own count of not-yet-dispatched orders: by_channel[].by_status{"Packed": {orders, items}}.
        days_ago=N keeps only orders created N or more days ago."""
        body = await self.request("GET", "/order_api/order_aging",
                                  params={"warehouse_id": warehouse_id, "days_ago": days_ago or None})
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            msg = body.get("message") if isinstance(body, dict) else ""
            raise OmsError(f"order_aging: unexpected answer {msg or type(body).__name__}")
        return data


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _is_throttle_body(resp: httpx.Response) -> bool:
    if resp.status_code != 200 or len(resp.content) > 200:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and str(body.get("error")) == "-4"


def _rows(body: Any) -> list[dict]:
    """OMSGuru returns either a bare array or an envelope {error, message, data}."""
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        err = body.get("error")
        if err not in (None, 0, "0", False) and not body.get("data"):
            msg = body.get("message") or f"error {err}"
            # "No records" style answers are not failures for list endpoints.
            if "no " in str(msg).lower() and "found" in str(msg).lower():
                return []
            raise OmsError(f"OMSGuru error {err}: {msg}")
        data = body.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for key in ("orders", "invoices", "rows", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]
        return []
    return []
