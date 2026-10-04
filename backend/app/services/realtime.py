"""In-process WebSocket fan-out so every station and dashboard sees scans live.

Run the server with a single worker (the default) — this hub and the OMS sync loop live in-process.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("realtime")

SEND_TIMEOUT = 2.0  # seconds a single client may take to accept one message


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def count(self) -> int:
        return len(self._clients)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def _send_all(self, text: str) -> None:
        """Send to every client at once; one stalled device (e.g. a phone on bad Wi-Fi) must not hold up the rest."""

        async def send(ws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(ws.send_text(text), SEND_TIMEOUT)
                return None
            except Exception:  # noqa: BLE001 - a closed or stalled socket must not break the broadcast
                return ws

        for ws in await asyncio.gather(*(send(ws) for ws in list(self._clients))):
            if ws is not None:
                self._clients.discard(ws)  # the page reconnects by itself

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """Safe to call from sync request handlers (threadpool) and from the event loop."""
        if not self._loop or not self._clients:
            return
        text = json.dumps({"event": event, "data": data}, default=str)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._loop.create_task(self._send_all(text))
        else:
            asyncio.run_coroutine_threadsafe(self._send_all(text), self._loop)


hub = Hub()
