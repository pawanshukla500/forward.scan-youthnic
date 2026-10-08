"""One-time mark of the AWBs OMSGuru already shipped (user, 8 Oct 2026): scanned on their OMSGuru ship date, under a
system account, counted apart - and a real scan of such a packet later replaces the mark (never "Duplicate")."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.main import app
from app.models import MARKED_SHIPPED_FLAG, SYSTEM_SHIPPED_USERNAME, Scan, ScanEvent, User
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS
from app.services import marking, reconcile
from app.timeutil import dispatch_date_for, from_unix, today_dispatch_date

CH = 0  # mock Myntra PPMP
CH_ID = int(MOCK_CHANNELS[CH]["id"])
OTHER_ID = int(MOCK_CHANNELS[1]["id"])


def run(coro):
    return asyncio.run(coro)


def _row(awb: str, order_id: str, status: str, shipped_ago_s: int | None = None) -> dict:
    c = MOCK_CHANNELS[CH]
    now = int(time.time())
    row = {
        "last_id": 6_300_000 + abs(hash(awb)) % 100_000, "invoice_id": f"INV/MRK/{order_id}",
        "invoice_date": now - 2 * 86400, "order_date": now - 2 * 86400 - 3600, "sla_date": now,
        "warehouse": "MAIN", "channel": c["name"], "company": c["company_name"], "shipment_tracker": awb,
        "shipping_company": "Myntra Logistics", "order_type": "Prepaid", "buyer_name": "Mark Test",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": f"{order_id}-1", "sku_code": "MRK-SKU",
                         "qty": 1, "invoice_amount": 799, "status": status}],
    }
    if shipped_ago_s is not None:
        row["shipment_date"] = now - shipped_ago_s
    return row


@pytest.fixture(scope="module")
def env():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        run(eng._guard("channels", eng.refresh_channels))
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        eng._apply_rows([
            _row("MRKSHIP0001", "ODMRK1", "Shipped", shipped_ago_s=86400),     # shipped yesterday, never scanned
            _row("MRKSHIP0002", "ODMRK2", "In Transit", shipped_ago_s=3 * 3600),
            _row("MRKOPEN0001", "ODMRK3", "Ready to ship"),                     # still to go: stays pending
            _row("MRKNODT0001", "ODMRK4", "Shipped"),                           # no ship date: not marked
            _row("MRKCANC0001", "ODMRK5", "Cancelled"),                         # cancelled: not pending anyway
        ], "invoices")
        yield c, eng


def _scan(awb: str) -> Scan | None:
    with session_scope() as db:
        return db.scalar(select(Scan).where(Scan.tracking_norm == awb))


def test_only_shipped_with_a_ship_date_are_candidates(env):
    with session_scope() as db:
        got = {r.awb for r, _ in marking.candidates(db)}
    assert {"MRKSHIP0001", "MRKSHIP0002"} <= got
    assert not {"MRKOPEN0001", "MRKNODT0001", "MRKCANC0001"} & got
    with session_scope() as db:  # --until before yesterday's ship day leaves it out
        early = {r.awb for r, _ in marking.candidates(db, until=today_dispatch_date().replace(day=1).replace(year=2000))}
    assert "MRKSHIP0001" not in early


def test_mark_scans_them_on_their_ship_date_under_the_system_account(env, monkeypatch):
    import mark_shipped_backlog as script

    c, _ = env
    before = c.get("/api/reconciliation").json()["totals"]
    monkeypatch.setattr("sys.argv", ["mark_shipped_backlog.py"])
    script.main()  # list only
    assert _scan("MRKSHIP0001") is None
    monkeypatch.setattr("sys.argv", ["mark_shipped_backlog.py", "--yes"])
    script.main()

    s = _scan("MRKSHIP0001")
    shipped = from_unix(int(time.time()) - 86400)
    assert s is not None and s.result == "OK" and MARKED_SHIPPED_FLAG in s.flags
    assert s.dispatch_date == dispatch_date_for(shipped) and s.station == marking.STATION
    with session_scope() as db:
        u = db.get(User, s.user_id)
        assert u.username == SYSTEM_SHIPPED_USERNAME and not u.is_active  # can never sign in
        ev = db.scalar(select(ScanEvent).where(ScanEvent.scan_id == s.id, ScanEvent.outcome == "MARKED_SHIPPED"))
        assert ev is not None
        recs = {r.awb: r for r in reconcile.collect(db)}
    assert recs["MRKSHIP0001"].bucket() == "scanned" and recs["MRKSHIP0001"].marked
    assert recs["MRKOPEN0001"].bucket() == "pending"
    assert _scan("MRKOPEN0001") is None and _scan("MRKNODT0001") is None

    with session_scope() as db:  # idempotent: nothing left to mark
        assert not {r.awb for r, _ in marking.candidates(db)} & {"MRKSHIP0001", "MRKSHIP0002"}
    after = c.get("/api/reconciliation").json()["totals"]
    assert after["marked_shipped"] >= before.get("marked_shipped", 0)


def test_a_real_scan_of_a_marked_packet_wrong_marketplace_keeps_the_mark(env):
    c, eng = env
    eng.client.busy = True
    try:
        res = c.post("/api/scan", json={"channel_id": OTHER_ID, "tracking": "MRKSHIP0002"}).json()
    finally:
        eng.client.busy = False
    assert res["code"] == "WRONG_CHANNEL", res
    assert MARKED_SHIPPED_FLAG in _scan("MRKSHIP0002").flags  # still counted as shipped, not back in pending


def test_a_real_scan_replaces_the_mark_and_is_ok_not_duplicate(env):
    c, eng = env
    eng.client.busy = True
    try:
        res = c.post("/api/scan", json={"channel_id": CH_ID, "tracking": "MRKSHIP0001"}).json()
    finally:
        eng.client.busy = False
    assert res["severity"] == "success" and res["code"] == "OK", res
    s = _scan("MRKSHIP0001")
    assert MARKED_SHIPPED_FLAG not in s.flags and s.dispatch_date == today_dispatch_date() and s.station != marking.STATION
    with session_scope() as db:
        assert db.scalar(select(ScanEvent).where(ScanEvent.tracking_norm == "MRKSHIP0001",
                                                 ScanEvent.outcome == "MARK_REPLACED")) is not None
    # and a second real scan is the usual duplicate
    eng.client.busy = True
    try:
        again = c.post("/api/scan", json={"channel_id": CH_ID, "tracking": "MRKSHIP0001"}).json()
    finally:
        eng.client.busy = False
    assert again["code"] == "DUPLICATE"
