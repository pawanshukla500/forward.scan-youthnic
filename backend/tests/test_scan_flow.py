import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.oms import sync as sync_module
from app.oms.mock import MOCK_CHANNELS, mock_awb


def _ch(i: int) -> int:
    return int(MOCK_CHANNELS[i % len(MOCK_CHANNELS)]["id"])


def _is_ready(i: int) -> bool:
    return i % 25 != 7 and i % 40 != 11


@pytest.fixture(scope="module")
def engine():
    eng = sync_module.SyncEngine()
    sync_module.engine_instance = eng

    async def seed():
        for name, fn in (("channels", eng.refresh_channels), ("invoices", eng.sync_invoices),
                         ("cancel_sweep", eng.cancel_sweep)):
            await eng._guard(name, fn)
        while True:
            await eng._guard("open_orders", eng.step_open_orders)
            if not sync_module._get_state("open_orders_progress"):
                break

    return eng, seed


@pytest.fixture(scope="module")
def client(engine):
    eng, seed = engine
    with TestClient(app) as c:
        asyncio.run(seed())
        r = c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"})
        assert r.status_code == 200, r.text
        yield c


def scan(c, channel_id, tracking, station="T1"):
    r = c.post("/api/scan", json={"channel_id": channel_id, "tracking": tracking, "station": station})
    assert r.status_code == 200, r.text
    return r.json()


def test_channels_synced(client):
    data = client.get("/api/channels").json()
    ids = {c["id"] for c in data["channels"]}
    assert {int(c["id"]) for c in MOCK_CHANNELS} <= ids
    assert sum(c["pending"] for c in data["channels"]) > 0


def test_ok_then_duplicate(client):
    i = 3
    assert _is_ready(i)
    res = scan(client, _ch(i), mock_awb(i))
    assert res["severity"] == "success", res
    assert res["order"]["channel_order_id"] == f"OD{900000 + i}"
    dup = scan(client, _ch(i), mock_awb(i).lower() + "  ")
    assert dup["severity"] == "error" and dup["code"] == "DUPLICATE"
    # duplicate wins even when scanned into a different channel
    dup2 = scan(client, _ch(i + 1), mock_awb(i))
    assert dup2["code"] == "DUPLICATE"


def test_wrong_channel_is_blocked_and_not_stored(client):
    i = 4
    res = scan(client, _ch(i + 1), mock_awb(i))
    assert res["severity"] == "error" and res["code"] == "WRONG_CHANNEL"
    ok = scan(client, _ch(i), mock_awb(i))
    assert ok["severity"] == "success"


def _cached(awb: str) -> int:
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import OmsOrder

    with session_scope() as db:
        return db.scalar(select(func.count(OmsOrder.id)).where(OmsOrder.tracking_norm == awb)) or 0


def test_cancelled_before_processing_is_not_synced(client):
    i = 7  # cancelled in OMS before it was ever packed / ready to ship
    assert _cached(mock_awb(i)) == 0
    res = scan(client, _ch(i), mock_awb(i))
    assert res["severity"] != "success", res


def test_new_orders_are_not_synced(client):
    i = 11  # still "New" in OMS - not part of the Packed / Ready-to-ship working set
    assert _cached(mock_awb(i)) == 0
    res = scan(client, _ch(i), mock_awb(i))
    assert res["severity"] == "error" and res["code"] == "NOT_IN_OMS" and "scan" not in res, res


def test_order_id_barcode_resolves_to_awb(client):
    i = 13
    res = scan(client, _ch(i), f"OD{900000 + i}")
    assert res["severity"] == "success"
    assert res["scan"]["tracking_norm"] == mock_awb(i)
    assert scan(client, _ch(i), mock_awb(i))["code"] == "DUPLICATE"


def test_not_found_is_not_saved_and_scans_ok_once_the_order_syncs(client, engine):
    """User, 9 Oct 2026: only scans that match a synced order of the selected marketplace are saved."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Scan, ScanEvent

    eng, _ = engine
    awb = "NEWAWB778899"
    res = scan(client, _ch(0), awb)
    assert res["severity"] == "error" and res["code"] == "NOT_IN_OMS" and "NOT FOUND - not saved" in res["message"]
    with session_scope() as db:
        assert db.scalar(select(Scan.id).where(Scan.tracking_norm == awb)) is None  # not in the scans
        ev = db.scalar(select(ScanEvent).where(ScanEvent.tracking_norm == awb))
        assert ev is not None and ev.outcome == "NOT_FOUND"  # but the attempt is on record
    row = {"last_id": 1, "invoice_id": "INV/X/1", "channel": MOCK_CHANNELS[0]["name"], "company": MOCK_CHANNELS[0]["company_name"],
           "shipment_tracker": awb, "shipping_company": "Delhivery", "order_type": "COD", "order_date": int(time.time()),
           "order_items": [{"channel_order_id": "ODNEW1", "channel_sub_order_id": "ODNEW1-1", "sku_code": "X", "qty": 1,
                            "invoice_amount": 100, "status": "Ready to ship"}]}
    eng._apply_rows([row], "invoices")
    again = scan(client, _ch(0), awb)  # the order has synced: the same packet now scans OK and is saved
    assert again["severity"] == "success" and again["scan"]["result"] == "OK", again
    assert again["order"]["channel_order_id"] == "ODNEW1"


def test_cancelled_after_scan_raises_alert(client, engine):
    eng, _ = engine
    awb = "LATECANCEL001"
    row = {"last_id": 2, "invoice_id": "INV/X/2", "channel": MOCK_CHANNELS[1]["name"], "company": MOCK_CHANNELS[1]["company_name"],
           "shipment_tracker": awb, "order_date": int(time.time()),
           "order_items": [{"channel_order_id": "ODLATE", "channel_sub_order_id": "ODLATE-1", "sku_code": "Y", "qty": 1,
                            "invoice_amount": 100, "status": "Packed"}]}
    eng._apply_rows([row], "invoices")
    assert scan(client, _ch(1), awb)["severity"] == "success"
    row["order_items"][0]["status"] = "Cancelled"
    eng._apply_rows([row], "cancel")
    s = client.get("/api/scans", params={"q": awb}).json()["scans"][0]
    assert s["alert"].startswith("AFTER SCAN") and "CANCELLED" in s["alert"]


def test_concurrent_duplicate_only_one_wins(client):
    i = 17
    results = []

    def worker():
        results.append(client.post("/api/scan", json={"channel_id": _ch(i), "tracking": mock_awb(i)}).json())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["severity"] == "success" for r in results) == 1
    assert sum(r.get("code") == "DUPLICATE" for r in results) == 5


def test_dashboard_and_exports(client):
    dash = client.get("/api/dashboard").json()
    assert dash["totals"]["scanned"] >= 5
    assert dash["totals"]["duplicate"] >= 1 and dash["totals"]["wrong_channel"] >= 1
    r = client.get("/api/scans/export.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    mans = client.get("/api/manifests").json()["manifests"]
    assert mans and all(m["status"] == "OPEN" for m in mans)
    mid = mans[0]["id"]
    assert client.post(f"/api/manifests/{mid}/close").status_code == 200
    r = client.get(f"/api/manifests/{mid}/export.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    csv_r = client.get("/api/oms-dispatch/export.csv", params={"mark": "true"})  # a GET only marks when asked
    assert csv_r.status_code == 200 and "Channel Order ID" in csv_r.text
    done = client.post("/api/oms-dispatch/mark-done", json={"date": dash["date"]}).json()
    assert done["updated"] >= 1


def test_closed_manifest_starts_new_batch(client):
    i = 19
    res = scan(client, _ch(i), mock_awb(i))
    assert res["severity"] == "success"
    mans = [m for m in client.get("/api/manifests").json()["manifests"] if m["channel_id"] == _ch(i)]
    assert any(m["status"] == "OPEN" for m in mans)


def test_scanner_role_limits(client):
    r = client.post("/api/admin/users", json={"username": "packer1", "full_name": "Packer One", "password": "pack-123456", "role": "scanner",
                                             "must_change_password": False})
    assert r.status_code == 200, r.text
    with TestClient(app) as c2:
        assert c2.post("/api/auth/login", json={"username": "packer1", "password": "pack-123456"}).status_code == 200
        assert c2.post("/api/admin/users", json={"username": "x1", "password": "xxxxxxx", "role": "admin"}).status_code == 403
        i = 23
        res = c2.post("/api/scan", json={"channel_id": _ch(i), "tracking": mock_awb(i)}).json()
        assert res["severity"] == "success"
        # scanner may undo own recent scan
        assert c2.delete(f"/api/scans/{res['scan']['id']}").status_code == 200
        res2 = c2.post("/api/scan", json={"channel_id": _ch(i), "tracking": mock_awb(i)}).json()
        assert res2["severity"] == "success"


def test_requires_login():
    with TestClient(app) as anon:
        assert anon.get("/api/channels").status_code == 401
        assert anon.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401


def test_tracking_normalization_with_courier_suffix(client, engine):
    from app.oms.mapping import normalize_tracking

    # Suffixes like '( EK_E2E )' or '(Delhivery)' must normalize to bare AWB
    assert normalize_tracking("MYEP1135031932 ( EK_E2E )") == "MYEP1135031932"
    assert normalize_tracking("delhivery_12345 (Delhivery)") == "DELHIVERY_12345"
    assert normalize_tracking("123456789") == "123456789"


def test_sku_photo_sync_and_rendering(client, engine):
    eng, _ = engine
    # Run sku_photos job
    asyncio.run(eng._guard("sku_photos", eng.refresh_sku_photos))

    # Scan an order and verify its items contain image_url
    i = 31
    res = scan(client, _ch(i), mock_awb(i))
    assert res["severity"] == "success"
    order = res.get("order")
    assert order is not None
    assert len(order["items"]) > 0
    # At least one item has image_url populated from SkuPhoto sync
    first_item = order["items"][0]
    assert "sku" in first_item
    assert "image_url" in first_item
    assert first_item["image_url"] is not None
    assert first_item["image_url"].startswith("https://")

