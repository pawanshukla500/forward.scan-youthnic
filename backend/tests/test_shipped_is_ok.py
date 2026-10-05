"""Shipped / In Transit in OMSGuru is a normal state for a packet being scanned, never a Check (5 Oct 2026):
Meesho (Valmo) labels show In Transit right after the AWB is made, and every scanned packet gets there after pickup."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Scan
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS
from app.services.scanning import clear_shipped_checks

CH = 1  # mock Meesho channel
CH_ID = int(MOCK_CHANNELS[CH]["id"])


def _row(awb: str, order_id: str, status: str) -> dict:
    c = MOCK_CHANNELS[CH]
    now = int(time.time())
    return {
        "last_id": 7_000_000 + abs(hash(awb)) % 1_000_000, "invoice_id": f"INV/{order_id}",
        "invoice_date": now - 120, "order_date": now - 7200, "sla_date": now + 86400,
        "warehouse": "MAIN", "channel": c["name"], "company": c["company_name"], "shipment_tracker": awb,
        "shipping_company": "Valmo", "order_type": "Prepaid", "buyer_name": "Ship Test",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": f"{order_id}-1", "sku_code": "SHIP-SKU",
                         "qty": 1, "invoice_amount": 499, "status": status}],
    }


@pytest.fixture(scope="module")
def env():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        asyncio.run(eng._guard("channels", eng.refresh_channels))
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c, eng


def _scan(c, eng, awb: str) -> dict:
    eng.client.busy = True  # answer from the local copy - no live lookup in this test
    try:
        r = c.post("/api/scan", json={"channel_id": CH_ID, "tracking": awb})
    finally:
        eng.client.busy = False
    assert r.status_code == 200, r.text
    return r.json()


def _stored(awb: str) -> Scan:
    with SessionLocal() as db:
        return db.scalar(select(Scan).where(Scan.tracking_norm == awb))


def test_in_transit_in_oms_scans_ok(env):
    c, eng = env
    eng._apply_rows([_row("SHIPOK0001", "ODSHIP1", "In Transit")], "invoices")
    flagged_before = c.get("/api/scan-context", params={"channel_id": CH_ID}).json()["stats"]["flagged"]
    res = _scan(c, eng, "SHIPOK0001")
    assert res["severity"] == "success" and res["code"] == "OK", res
    assert res["scan"]["result"] == "OK" and res["scan"]["flags"] == []
    stats = c.get("/api/scan-context", params={"channel_id": CH_ID}).json()["stats"]
    assert stats["flagged"] == flagged_before  # not counted as "needs review"


def test_scan_stays_ok_when_the_order_ships_after_the_scan(env):
    c, eng = env
    eng._apply_rows([_row("SHIPOK0002", "ODSHIP2", "Ready to ship")], "invoices")
    assert _scan(c, eng, "SHIPOK0002")["code"] == "OK"
    # courier pickup: the next sync brings the order back as In Transit and re-checks the scan
    eng._apply_rows([_row("SHIPOK0002", "ODSHIP2", "In Transit")], "invoices")
    s = _stored("SHIPOK0002")
    assert s.result == "OK" and not s.flags and not s.alert


def test_old_shipped_checks_are_cleared(env):
    c, eng = env
    eng._apply_rows([_row("SHIPOK0003", "ODSHIP3", "In Transit"), _row("SHIPOK0004", "ODSHIP4", "In Transit")], "invoices")
    _scan(c, eng, "SHIPOK0003")
    _scan(c, eng, "SHIPOK0004")
    # as saved before the change: one scan with only the shipped check, one with a real check as well
    with SessionLocal() as db:
        a = db.scalar(select(Scan).where(Scan.tracking_norm == "SHIPOK0003"))
        a.result, a.flags, a.message = "WARN", "ALREADY_SHIPPED_IN_OMS", "OMS already shows this order as shipped"
        b = db.scalar(select(Scan).where(Scan.tracking_norm == "SHIPOK0004"))
        b.result, b.flags = "WARN", "NOT_RTS,ALREADY_SHIPPED_IN_OMS"
        b.message = "OMS still shows this order as New/Pending (not packed); OMS already shows this order as shipped"
        db.commit()
    with SessionLocal() as db:
        assert clear_shipped_checks(db) == 2
    a, b = _stored("SHIPOK0003"), _stored("SHIPOK0004")
    assert (a.result, a.flags, a.message) == ("OK", "", "Verified")
    assert (b.result, b.flags) == ("WARN", "NOT_RTS") and b.message == "OMS still shows this order as New/Pending (not packed)"
    with SessionLocal() as db:
        assert clear_shipped_checks(db) == 0  # idempotent
