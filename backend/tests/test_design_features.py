"""Endpoints behind the ForwardScan design: scan context, flagging, marketplaces, reports tabs, dashboard metrics."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS

CH = 5  # mock "VB EXPORT - Shopify"
CH_ID = int(MOCK_CHANNELS[CH]["id"])


def _row(awb, order_id, status="Ready to ship", items=(("SKU-A", 1),), sla_in_s=7200, invoice_age_s=120, courier="Delhivery"):
    c = MOCK_CHANNELS[CH]
    now = int(time.time())
    return {
        "last_id": 6_000_000 + abs(hash(awb)) % 1_000_000, "invoice_id": f"INV/{order_id}", "invoice_date": now - invoice_age_s,
        "order_date": now - invoice_age_s - 3600, "sla_date": now + sla_in_s, "warehouse": "MAIN", "channel": c["name"],
        "company": c["company_name"], "shipment_tracker": awb, "shipping_company": courier, "order_type": "COD",
        "buyer_name": "Design Test", "buyer_city": "Pune", "buyer_pincode": "411001",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": f"{order_id}-{i}", "sku_code": sku, "qty": q,
                         "invoice_amount": 500 * q, "status": status} for i, (sku, q) in enumerate(items, start=1)],
    }


@pytest.fixture(scope="module")
def env():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        asyncio.run(eng._guard("channels", eng.refresh_channels))
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        eng._apply_rows([
            _row("DSGMULTI01", "ODDSG1", items=(("TEE-BLK-L", 1), ("CAP-NVY", 1), ("SOCK-WHT", 2))),
            _row("DSGURGENT1", "ODDSG2", sla_in_s=-600),                 # SLA already passed
            _row("DSGHIGH001", "ODDSG3", sla_in_s=3600, courier="Ekart"),  # SLA within 3h
            _row("DSGOLD0001", "ODDSG4", invoice_age_s=2 * 86400),        # AWB from 2 days ago
        ], "invoices")
        yield c, eng


def test_scan_context_queue_and_priorities(env):
    c, _ = env
    ctx = c.get("/api/scan-context", params={"channel_id": CH_ID}).json()
    q = {r["awb"]: r for r in ctx["queue"]}
    assert q["DSGURGENT1"]["priority"] == "Urgent" and q["DSGOLD0001"]["priority"] == "Urgent"
    assert q["DSGHIGH001"]["priority"] == "High"
    assert q["DSGMULTI01"]["skus"] == 3 and q["DSGMULTI01"]["units"] == 4
    assert ctx["awb"]["overdue"] >= 1 and ctx["awb"]["pending"] >= 3
    assert ctx["pending_by_courier"].get("Ekart", 0) >= 1


def test_multi_sku_scan_flag_and_manifest_state(env):
    c, eng = env
    eng.client.busy = True
    try:
        res = c.post("/api/scan", json={"channel_id": CH_ID, "tracking": "DSGMULTI01"}).json()
    finally:
        eng.client.busy = False
    assert res["severity"] == "success"
    assert len(res["order"]["items"]) == 3 and res["order"]["total_qty"] == 4
    assert res["scan"]["manifest"]["status"] == "OPEN"
    f = c.post(f"/api/scans/{res['scan']['id']}/flag", json={"reason": "Missing item", "note": "socks short"}).json()
    assert "FLAGGED" in f["scan"]["flags"] and f["scan"]["result"] == "WARN" and "Missing item" in f["scan"]["message"]
    ctx = c.get("/api/scan-context", params={"channel_id": CH_ID}).json()
    assert ctx["stats"]["flagged_manual"] >= 1 and ctx["stats"]["scanned"] >= 1
    flagged = c.get("/api/scans", params={"result": "FLAGGED", "channel_id": CH_ID}).json()["scans"]
    assert any(s["tracking_norm"] == "DSGMULTI01" for s in flagged)
    by_courier = c.get("/api/scans", params={"courier": "Delhivery", "channel_id": CH_ID}).json()["scans"]
    assert any(s["tracking_norm"] == "DSGMULTI01" for s in by_courier)
    assert c.get("/api/scans", params={"courier": "Ekart", "q": "DSGMULTI01"}).json()["total"] == 0


def test_reports_tabs_and_exports(env):
    c, _ = env
    sku = c.get("/api/reports/sku-summary").json()["rows"]
    tee = next(r for r in sku if r["sku"] == "TEE-BLK-L")
    assert tee["scanned"] >= 1 and tee["top_channel"]
    ops = c.get("/api/reports/operators").json()["rows"]
    assert ops and ops[0]["scans"] >= 1 and ops[0]["success_rate"] is not None
    filt = c.get("/api/reports/filters").json()
    assert "Delhivery" in filt["couriers"] and filt["operators"]
    csv = c.get("/api/scans/export.csv")
    assert csv.status_code == 200 and "AWB / Tracking" in csv.text


def test_marketplaces_and_dashboard_metrics(env):
    c, _ = env
    m = c.get("/api/marketplaces").json()
    shop = next(x for x in m["channels"] if x["id"] == CH_ID)
    assert shop["awb_7d"] >= 4 and shop["last_synced_at"] and shop["today"]["generated"] >= 3
    d = c.get("/api/dashboard").json()["metrics"]
    assert {"success_rate", "flagged", "rejected", "avg_scan_seconds", "change_vs_last_week"} <= set(d)


def test_session_only_login_cookie():
    with TestClient(app) as anon:
        r = anon.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass", "remember": False})
        assert r.status_code == 200
        assert "max-age" not in r.headers["set-cookie"].lower()
