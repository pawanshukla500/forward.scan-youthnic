"""8 Oct 2026: other barcodes on a label / packet (Myntra packet id, 2-D route code, product EAN) are rejected as
"WRONG BARCODE" instead of being saved as "Not found"; an order OMS already moved past Ready-to-ship scans OK."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, session_scope
from app.main import app
from app.models import OmsOrder, Scan, ScanEvent
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS
from app.services import awb_shapes
from app.services.scanning import clear_shipped_checks

CH = 3  # mock Ajio channel: AWBs here look like TSTX + 10 digits (taught below)
CH_ID = int(MOCK_CHANNELS[CH]["id"])
SMALL = 4  # Easyship: too few synced AWBs to know its format -> no guard


def _row(awb: str, order_id: str, status: str = "Ready to ship", ch: int = CH) -> dict:
    c = MOCK_CHANNELS[ch]
    now = int(time.time())
    return {
        "last_id": 9_000_000 + abs(hash(awb)) % 1_000_000, "invoice_id": f"INV/{order_id}",
        "invoice_date": now - 120, "order_date": now - 7200, "sla_date": now + 86400,
        "warehouse": "MAIN", "channel": c["name"], "company": c["company_name"], "shipment_tracker": awb,
        "shipping_company": "Test Courier", "order_type": "Prepaid", "buyer_name": "Shape Test",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": f"{order_id}-1", "sku_code": "SHAPE-SKU",
                         "qty": 1, "invoice_amount": 399, "status": status}],
    }


@pytest.fixture(scope="module")
def env():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        asyncio.run(eng._guard("channels", eng.refresh_channels))
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        eng._apply_rows([_row(f"TSTX{4300000000 + i}", f"ODSHAPE{i}") for i in range(120)], "invoices")
        awb_shapes.reset()
        try:
            yield c, eng
        finally:  # leave the other tests' channels as they were
            with session_scope() as db:
                db.execute(delete(Scan).where(Scan.tracking_norm.like("TSTX%")))
                db.execute(delete(OmsOrder).where(OmsOrder.tracking_norm.like("TSTX%")))
            awb_shapes.reset()


def _scan(c, eng, code: str, ch_id: int = CH_ID) -> dict:
    eng.client.busy = True  # local copy only - no live lookup
    try:
        r = c.post("/api/scan", json={"channel_id": ch_id, "tracking": code})
    finally:
        eng.client.busy = False
    assert r.status_code == 200, r.text
    return r.json()


def _saved(code: str) -> bool:
    with SessionLocal() as db:
        return db.scalar(select(Scan.id).where(Scan.tracking_raw == code)) is not None


def test_shape_and_description():
    assert awb_shapes.shape("FMPC6580571822") == "AAAA9999999999"
    assert awb_shapes.describe("FMPC6580571822") == "FMPC + 10 digits"
    assert awb_shapes.describe("374012345678") == "12 digits"
    assert awb_shapes.describe("AM123456789IN") == "AM + 9 digits + IN"
    assert awb_shapes.has_symbols("5|\\MB-2141061574564445|O|NAG/WRA|S|E|03|E|S|F|77")
    assert awb_shapes.has_symbols('2JP"6576884942') and awb_shapes.has_symbols("FMP=43299415:/")
    assert not awb_shapes.has_symbols("FMPC6580571822") and not awb_shapes.has_symbols("VL0012345678901")


@pytest.mark.parametrize("code", ["MPP3EM005150828", "8901234567890", "DB018139116", "SPTSB2A083596749"])
def test_other_barcodes_are_rejected_not_saved(env, code):
    c, eng = env
    res = _scan(c, eng, code)
    assert res["severity"] == "error" and res["code"] == "WRONG_BARCODE", res
    assert "not an AWB" in res["message"] and "TSTX + 10 digits" in res["message"]
    assert not _saved(code)
    with SessionLocal() as db:  # still in the audit trail
        ev = db.scalar(select(ScanEvent).where(ScanEvent.tracking_raw == code).order_by(ScanEvent.id.desc()))
        assert ev.outcome == "INVALID"


def test_route_code_is_rejected_without_a_lookup(env):
    c, eng = env
    code = "5|\\MB-2141061574564445|O|NAG/WRA|S|E|03|E|S|F|77"
    res = _scan(c, eng, code)
    assert res["code"] == "WRONG_BARCODE" and "route" in res["message"]
    assert not _saved(code)


def test_real_looking_awb_that_is_not_in_oms_is_still_accepted(env):
    c, eng = env
    res = _scan(c, eng, "TSTX9999999999")
    assert res["severity"] == "warning" and res["code"] == "NOT_IN_OMS", res
    assert _saved("TSTX9999999999")


def test_order_id_barcode_of_a_known_order_is_not_a_wrong_barcode(env):
    c, eng = env
    res = _scan(c, eng, "ODSHAPE7")
    assert res["severity"] == "success", res
    assert res["scan"]["tracking_norm"] == "TSTX4300000007"  # saved under the AWB


def test_channels_without_enough_orders_are_not_guarded(env):
    c, eng = env
    res = _scan(c, eng, "MPP3EM005150999", ch_id=int(MOCK_CHANNELS[SMALL]["id"]))
    assert res["code"] == "NOT_IN_OMS"


def test_order_that_left_ready_to_ship_scans_ok(env):
    c, eng = env
    eng._apply_rows([_row("TSTX5000000001", "ODMOVED1")], "invoices")
    with session_scope() as db:
        o = db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == "TSTX5000000001"))
        o.status_group = "MOVED"  # left Packed / Ready-to-ship, status not fetched yet
    res = _scan(c, eng, "TSTX5000000001")
    assert res["severity"] == "success" and res["code"] == "OK", res


def test_old_status_changed_checks_become_ok(env):
    c, eng = env
    eng._apply_rows([_row("TSTX5000000002", "ODMOVED2")], "invoices")
    assert _scan(c, eng, "TSTX5000000002")["code"] == "OK"
    with session_scope() as db:
        s = db.scalar(select(Scan).where(Scan.tracking_norm == "TSTX5000000002"))
        s.result, s.flags, s.message = "WARN", "STATUS_CHANGED", "Order is no longer Ready-to-ship in OMS - verify status"
    with SessionLocal() as db:
        assert clear_shipped_checks(db) >= 1
    with SessionLocal() as db:
        s = db.scalar(select(Scan).where(Scan.tracking_norm == "TSTX5000000002"))
        assert (s.result, s.flags, s.message) == ("OK", "", "Verified")


def test_cleanup_removes_old_wrong_barcode_scans_and_keeps_real_ones(env, monkeypatch):
    import cleanup_wrong_barcodes as cleanup
    from app.models import User
    from app.timeutil import today_dispatch_date

    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        for code in ("MPP3EM000000001", "TSTX1111111111"):  # saved as "Not found" before the guard existed
            db.add(Scan(tracking_norm=code, tracking_raw=code, dispatch_date=today_dispatch_date(), user_id=admin.id,
                        channel_id=CH_ID, result="UNVERIFIED", flags="NOT_IN_OMS"))
    with session_scope() as db:
        found = {s.tracking_norm for s, _ in cleanup.candidates(db, days=1)}
    assert "MPP3EM000000001" in found and "TSTX1111111111" not in found

    monkeypatch.setattr("sys.argv", ["cleanup_wrong_barcodes.py"])  # list only: nothing changes
    cleanup.main()
    assert _saved("MPP3EM000000001")
    monkeypatch.setattr("sys.argv", ["cleanup_wrong_barcodes.py", "--yes", "--days", "1"])
    cleanup.main()
    assert not _saved("MPP3EM000000001") and _saved("TSTX1111111111")
    with SessionLocal() as db:
        ev = db.scalar(select(ScanEvent).where(ScanEvent.tracking_norm == "MPP3EM000000001", ScanEvent.outcome == "VOIDED"))
        assert ev is not None and "wrong barcode" in ev.message
