"""AWBs generated vs scanned, per channel: pending today, overdue, cancelled after AWB. An AWB stays pending until it
is scanned here, also when OMS already shows it shipped (counted in pending, and as "left_unscanned" for information)."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS

CH = 4  # mock "VB EXPORT - Amazon - Easyship"
CH_ID = int(MOCK_CHANNELS[CH]["id"])


def _row(awb: str, order_id: str, status: str, invoice_age_s: int = 120) -> dict:
    c = MOCK_CHANNELS[CH]
    now = int(time.time())
    return {
        "last_id": 8_000_000 + abs(hash(awb)) % 1_000_000, "invoice_id": f"INV/{order_id}",
        "invoice_date": now - invoice_age_s, "order_date": now - invoice_age_s - 7200, "sla_date": now + 86400,
        "warehouse": "MAIN", "channel": c["name"], "company": c["company_name"], "shipment_tracker": awb,
        "shipping_company": "Amazon Shipping", "order_type": "Prepaid", "buyer_name": "Rec Test",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": f"{order_id}-1", "sku_code": "REC-SKU",
                         "qty": 1, "invoice_amount": 650, "status": status}],
    }


@pytest.fixture(scope="module")
def env():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        asyncio.run(eng._guard("channels", eng.refresh_channels))
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c, eng


def _ch(summary: dict) -> dict:
    return next((x for x in summary["channels"] if x["id"] == CH_ID),
                {"generated": 0, "scanned": 0, "pending": 0, "overdue": 0, "left_unscanned": 0, "cancelled": 0,
                 "pending_all": 0, "synced": 0})


def test_reconciliation_buckets(env):
    c, eng = env
    before = _ch(c.get("/api/reconciliation").json())
    # some AWB times are a few minutes ago, so a test run just after midnight can't straddle two days
    eng._apply_rows([
        _row("RECPEND001", "ODREC1", "Ready to ship"),                         # due today, not scanned
        _row("RECSCAN001", "ODREC2", "Ready to ship"),                         # will be scanned
        _row("RECLEFT001", "ODREC3", "In Transit"),                            # OMS shipped it, never scanned: pending
        _row("RECCANC001", "ODREC4", "Cancelled Before Shipping"),             # cancelled after AWB
        _row("RECOVER001", "ODREC5", "Ready to ship", invoice_age_s=2 * 86400),  # AWB 2 days ago, still waiting
    ], "invoices")
    eng.client.busy = True
    try:
        res = c.post("/api/scan", json={"channel_id": CH_ID, "tracking": "RECSCAN001"}).json()
    finally:
        eng.client.busy = False
    assert res["severity"] == "success"
    assert res["order"]["dispatch_due"]["state"] == "today"

    after = _ch(c.get("/api/reconciliation").json())
    delta = {k: after[k] - before[k] for k in ("generated", "scanned", "pending", "overdue", "left_unscanned", "cancelled")}
    # pending = RECPEND001 + RECLEFT001 (OMS says In Transit, but nobody scanned it); left_unscanned is part of it
    assert delta == {"generated": 4, "scanned": 1, "pending": 2, "overdue": 1, "left_unscanned": 1, "cancelled": 1}
    # the owner's simple calculation (10 Oct 2026): SYNCED - SCANNED = PENDING, the earlier day's AWB included
    assert after["synced"] - after["scanned"] == after["pending_all"]
    assert after["pending_all"] - before["pending_all"] == 3 and after["synced"] - before["synced"] == 4

    pend = c.get("/api/reconciliation/list", params={"bucket": "pending", "channel_id": CH_ID}).json()["rows"]
    assert {"RECPEND001", "RECLEFT001", "RECOVER001"} <= {r["awb"] for r in pend}  # one list, earlier days too
    assert next(r for r in pend if r["awb"] == "RECLEFT001")["shipped_in_oms"] is True
    over = c.get("/api/reconciliation/list", params={"bucket": "overdue", "channel_id": CH_ID}).json()["rows"]
    row = next(r for r in over if r["awb"] == "RECOVER001")
    assert row["bucket"] == "pending" and row["age_days"] == 2  # no separate "overdue" any more
    left = c.get("/api/reconciliation/list", params={"bucket": "left_unscanned", "channel_id": CH_ID}).json()["rows"]
    assert "RECLEFT001" in {r["awb"] for r in left}
    scanned = c.get("/api/reconciliation/list", params={"bucket": "scanned", "channel_id": CH_ID}).json()["rows"]
    assert next(r for r in scanned if r["awb"] == "RECSCAN001")["scan"]["user"]

    x = c.get("/api/reconciliation/export.xlsx", params={"bucket": "overdue"})
    assert x.status_code == 200 and x.content[:2] == b"PK"

    chans = c.get("/api/channels").json()["channels"]
    mine = next(ch for ch in chans if ch["id"] == CH_ID)
    assert mine["awb_today"]["generated"] >= 4 and mine["awb_today"]["overdue"] >= 1

    # the scan station's queue (web + phone Pending list) holds the shipped-but-unscanned AWB too ...
    ctx = c.get("/api/scan-context", params={"channel_id": CH_ID, "limit": 50}).json()
    assert "RECLEFT001" in {q["awb"] for q in ctx["queue"]}
    # ... until it is scanned: then it scans OK and leaves pending
    eng.client.busy = True
    try:
        res = c.post("/api/scan", json={"channel_id": CH_ID, "tracking": "RECLEFT001"}).json()
    finally:
        eng.client.busy = False
    assert res["severity"] == "success", res
    again = _ch(c.get("/api/reconciliation").json())
    assert again["pending"] == after["pending"] - 1 and again["left_unscanned"] == after["left_unscanned"] - 1

    # an earlier day's AWB scanned today: one order moves from pending to scanned, synced stays the same
    eng.client.busy = True
    try:
        res = c.post("/api/scan", json={"channel_id": CH_ID, "tracking": "RECOVER001"}).json()
    finally:
        eng.client.busy = False
    assert res["severity"] in ("success", "warning"), res
    late = _ch(c.get("/api/reconciliation").json())
    assert late["synced"] == again["synced"] and late["scanned"] == again["scanned"] + 1
    assert late["pending_all"] == again["pending_all"] - 1 and late["synced"] - late["scanned"] == late["pending_all"]
    scanned = c.get("/api/reconciliation/list", params={"bucket": "scanned", "channel_id": CH_ID}).json()["rows"]
    assert "RECOVER001" in {r["awb"] for r in scanned}
    awb = c.get("/api/scan-context", params={"channel_id": CH_ID}).json()["awb"]
    assert (awb["synced"], awb["scanned"], awb["pending_all"]) == (late["synced"], late["scanned"], late["pending_all"])


def test_channel_summary_day_and_month(env):
    c, _ = env
    day = c.get("/api/reports/channel-summary", params={"group": "day"}).json()
    assert day["grand_total"] >= 1 and any(ch["id"] == CH_ID for ch in day["channels"])
    month = c.get("/api/reports/channel-summary", params={"group": "month"}).json()
    assert month["grand_total"] == day["grand_total"]
    x = c.get("/api/reports/channel-summary.xlsx", params={"group": "month"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
