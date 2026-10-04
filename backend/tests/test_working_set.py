"""Sync follows Packed / Ready-to-ship orders; every AWB is kept RETAIN_ORDERS_DAYS (7) for reconciliation;
scans keep their own order details for the long term."""
import asyncio
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.main import app
from app.models import OmsOrder, Scan
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS
from app.timeutil import utcnow


def _row(awb: str, order_id: str, status: str, ch: int = 0, sub: str = "", invoice_age_s: int = 60) -> dict:
    c = MOCK_CHANNELS[ch]
    now = int(time.time())
    return {
        "last_id": 7_000_000 + abs(hash(awb)) % 1_000_000, "invoice_id": f"INV/{order_id}",
        "invoice_date": now - invoice_age_s, "order_date": now - invoice_age_s - 3600, "warehouse": "MAIN",
        "channel": c["name"], "company": c["company_name"], "shipment_tracker": awb, "shipping_company": "Ekart",
        "order_type": "COD", "buyer_name": "WS Test",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": sub or f"{order_id}-1",
                         "sku_code": "WS-SKU", "qty": 1, "invoice_amount": 500, "status": status}],
    }


def _get(awb: str) -> OmsOrder | None:
    with session_scope() as db:
        return db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == awb))


@pytest.fixture(scope="module")
def env():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        asyncio.run(eng._guard("channels", eng.refresh_channels))
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c, eng


def test_awbs_of_the_last_7_days_are_kept_whatever_their_status(env):
    _, eng = env
    week = sm.settings.retain_orders_days * 86400
    rows = [_row("WSRTS0001", "ODWS1", "Ready to ship"), _row("WSPACK0001", "ODWS2", "Packed"),
            _row("WSTRANS001", "ODWS3", "In Transit"), _row("WSCBS00001", "ODWS5", "Cancelled Before Shipping"),
            _row("WSOLD00001", "ODWS8", "Delivered", invoice_age_s=week + 3600),   # AWB older than 7 days
            _row("", "ODWS9", "Cancelled")]                                        # never got an AWB
    stored, _ = eng._apply_rows(rows, "invoices")
    assert stored == 4
    assert _get("WSRTS0001").awb_generated_at is not None
    assert _get("WSTRANS001").status_group == "SHIPPED" and _get("WSCBS00001").status_group == "CANCELLED"
    assert _get("WSOLD00001") is None
    with session_scope() as db:
        assert db.scalar(select(OmsOrder).where(OmsOrder.channel_order_id == "ODWS9")) is None


def test_ready_to_ship_order_that_gets_cancelled_is_updated_and_blocked(env):
    c, eng = env
    eng._apply_rows([_row("WSCANC0001", "ODWSC1", "Ready to ship")], "invoices")
    eng._apply_rows([_row("WSCANC0001", "ODWSC1", "Cancelled Before Shipping")], "invoices")
    o = _get("WSCANC0001")
    assert o.status_group == "CANCELLED" and o.left_at is not None
    eng.client.busy = True  # judge on the stored copy only
    try:
        res = c.post("/api/scan", json={"channel_id": int(MOCK_CHANNELS[0]["id"]), "tracking": "WSCANC0001"}).json()
    finally:
        eng.client.busy = False
    assert res["severity"] == "error" and res["code"] == "CANCELLED"


def test_cancel_check_only_touches_stored_orders(env):
    _, eng = env
    eng._apply_rows([_row("WSCC000001", "ODWSCC1", "Packed")], "invoices")
    known = _row("", "ODWSCC1", "Cancelled", sub="ODWSCC1-1")      # cancellation rows usually have no AWB
    unknown = _row("", "ODWSCC2", "Cancelled", sub="ODWSCC2-1")    # cancelled before processing
    stored, _ = eng._apply_rows([known, unknown], "cancel")
    assert stored == 1
    assert _get("WSCC000001").status_group == "CANCELLED"
    with session_scope() as db:
        assert db.scalar(select(OmsOrder).where(OmsOrder.channel_order_id == "ODWSCC2")) is None


def test_order_leaving_ready_to_ship_stops_syncing_and_ages_out_scans_keep_details(env):
    c, eng = env
    eng._apply_rows([_row("WSLEFT0001", "ODWSL1", "Ready to ship"), _row("WSKEEP0001", "ODWSK1", "Ready to ship")],
                    "invoices")
    res = c.post("/api/scan", json={"channel_id": int(MOCK_CHANNELS[0]["id"]), "tracking": "WSLEFT0001"}).json()
    assert res["severity"] == "success"
    # A full Packed / Ready-to-ship refresh that no longer contains WSLEFT0001 (it shipped)
    started = int(time.time())
    eng._apply_rows([_row("WSKEEP0001", "ODWSK1", "Ready to ship")], "orders")
    with session_scope() as db:
        db.get(OmsOrder, _get("WSLEFT0001").id).seen_open_at = utcnow() - timedelta(minutes=5)
    assert eng._mark_moved({"run_started": started}) >= 1
    left = _get("WSLEFT0001")
    assert left.status_group == "MOVED" and left.left_at is not None
    # still inside the 7 days: kept
    sm.prune_orders()
    assert _get("WSLEFT0001") is not None
    # AWB older than 7 days: removed; the still-open one stays (it is pending) even when just as old
    with session_scope() as db:
        old = utcnow() - timedelta(days=sm.settings.retain_orders_days, hours=1)
        db.get(OmsOrder, left.id).awb_generated_at = old
        db.get(OmsOrder, _get("WSKEEP0001").id).awb_generated_at = old
    assert sm.prune_orders() >= 1
    assert _get("WSLEFT0001") is None and _get("WSKEEP0001") is not None
    with session_scope() as db:
        s = db.scalar(select(Scan).where(Scan.tracking_norm == "WSLEFT0001"))
        assert s.order_id is None and "ODWSL1" in s.order_json
    found = c.get("/api/scans", params={"q": "ODWSL1"}).json()["scans"]
    assert found and found[0]["order"]["channel_order_id"] == "ODWSL1"
    export = c.get("/api/scans/export.xlsx", params={"q": "WSLEFT0001"})
    assert export.status_code == 200 and export.content[:2] == b"PK"


def test_scans_are_kept_for_the_retention_period(env):
    c, eng = env
    eng._apply_rows([_row("WSOLDSCAN1", "ODWSOS1", "Ready to ship")], "invoices")
    sid = c.post("/api/scan", json={"channel_id": int(MOCK_CHANNELS[0]["id"]), "tracking": "WSOLDSCAN1"}).json()["scan"]["id"]
    assert sm.prune_scans() == 0  # nothing is anywhere near 3 years old
    with session_scope() as db:
        db.get(Scan, sid).dispatch_date = db.get(Scan, sid).dispatch_date - timedelta(days=sm.settings.scan_retention_days + 1)
    assert sm.prune_scans() == 1
    with session_scope() as db:
        assert db.get(Scan, sid) is None
