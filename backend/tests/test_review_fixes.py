"""Review of the last merged PRs (8 Oct 2026): a re-made label's old AWB, the packer's own repeat scan, the audit's
"nothing extra" check, re-checking shipped-but-unscanned AWBs, spare-credit calls on a 429, restore -> deep audit,
text longer than its column."""
import asyncio
import dataclasses
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import session_scope
from app.main import app
from app.models import OmsOrder, Scan, User
from app.oms import client as client_module
from app.oms import sync as sm
from app.oms.client import OmsClient, OmsThrottled
from app.oms.mock import MOCK_CHANNELS
from app.security import hash_password
from app.services import reconcile, scanning
from app.timeutil import utcnow

CH = MOCK_CHANNELS[2]
CID = int(CH["id"])


def _row(awb: str, order_id: str, sub: str, status: str = "Ready to ship", **kw) -> dict:
    now = int(time.time())
    row = {
        "last_id": 7_000_000 + abs(hash(awb)) % 100_000, "invoice_id": f"INV/RF/{awb}", "invoice_date": now - 3600,
        "order_date": now - 7200, "warehouse": "MAIN", "channel": CH["name"], "company": CH["company_name"],
        "shipment_tracker": awb, "shipping_company": "Ekart", "order_type": "Prepaid", "buyer_name": "Review Test",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": sub, "sku_code": "RF-SKU", "qty": 1,
                         "invoice_amount": 100, "status": status}],
    }
    row.update(kw)
    return row


@pytest.fixture(scope="module")
def eng():
    e = sm.SyncEngine()
    sm.engine_instance = e
    with TestClient(app):
        asyncio.run(e._guard("channels", e.refresh_channels))
        yield e


def _user(name: str) -> int:
    with session_scope() as db:
        u = db.scalar(select(User).where(User.username == name))
        if u is None:
            u = User(username=name, full_name=name, role="scanner", password_hash=hash_password("Review-pass-2026"))
            db.add(u)
            db.flush()
        return u.id


def _scan(user_id: int, awb: str) -> dict:
    with session_scope() as db:
        return scanning.process_scan(db, user=db.get(User, user_id), channel_id=CID, raw=awb, station="RF")


def _order(awb: str) -> OmsOrder | None:
    with session_scope() as db:
        o = db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == awb))
        if o is not None:
            db.expunge(o)
        return o


def _pending() -> set[str]:
    with session_scope() as db:
        return {r.awb for r in reconcile.collect(db, pending_only=True)}


def test_old_awb_of_a_re_made_label_leaves_pending_and_is_refused(eng):
    eng._apply_rows([_row("RFOLD000001", "ODRF1", "ODRF1-1")], "invoices")
    assert "RFOLD000001" in _pending()
    # OMSGuru re-made the label: same order and sub-order, new AWB
    eng._apply_rows([_row("RFNEW000001", "ODRF1", "ODRF1-1")], "invoices")
    old = _order("RFOLD000001")
    assert old.status_group == "REPLACED" and "RFNEW000001" in old.status_text
    pend = _pending()
    assert "RFNEW000001" in pend and "RFOLD000001" not in pend  # one shipment, counted once
    # an old invoice row that still carries the old AWB does not bring it back
    eng._apply_rows([_row("RFOLD000001", "ODRF1", "ODRF1-1", status="Shipped")], "invoices")
    assert _order("RFOLD000001").status_group == "REPLACED"
    packer = _user("rf.packer1")
    res = _scan(packer, "RFOLD000001")
    assert res["severity"] == "error" and res["code"] == "REPLACED" and "OLD LABEL" in res["message"], res
    assert _scan(packer, "RFNEW000001")["severity"] == "success"


def test_separate_shipments_and_scanned_awbs_are_never_retired(eng):
    # one order, two sub-orders shipped separately: two real shipments
    eng._apply_rows([_row("RFMULTI0001", "ODRF2", "ODRF2-1")], "invoices")
    eng._apply_rows([_row("RFMULTI0002", "ODRF2", "ODRF2-2")], "invoices")
    assert {"RFMULTI0001", "RFMULTI0002"} <= _pending()
    # an AWB that already went out under its label keeps its history
    eng._apply_rows([_row("RFSCAN00001", "ODRF3", "ODRF3-1")], "invoices")
    assert _scan(_user("rf.packer1"), "RFSCAN00001")["severity"] == "success"
    eng._apply_rows([_row("RFSCAN00002", "ODRF3", "ODRF3-1")], "invoices")
    assert _order("RFSCAN00001").status_group != "REPLACED"


def test_own_repeat_scan_is_already_saved_not_duplicate(eng, monkeypatch):
    monkeypatch.setattr(scanning, "REPEAT_SECONDS", 120)
    eng._apply_rows([_row("RFREP000001", "ODRF4", "ODRF4-1")], "invoices")
    a, b = _user("rf.packer1"), _user("rf.packer2")
    assert _scan(a, "RFREP000001")["code"] == "OK"
    again = _scan(a, "RFREP000001")  # no answer in time on the phone, scanned again
    assert again["severity"] == "success" and again["code"] == "ALREADY_SAVED", again
    assert _scan(b, "RFREP000001")["code"] == "DUPLICATE"  # someone else: a real duplicate packet
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Scan).where(Scan.tracking_norm == "RFREP000001")) == 1


def test_shipped_but_unscanned_awbs_are_asked_about_again(eng):
    eng._apply_rows([_row("RFEXIT00001", "ODRF6", "ODRF6-1", status="Shipped")], "invoices")

    def due() -> bool:
        with session_scope() as db:
            return "RFEXIT00001" in set(db.scalars(select(OmsOrder.tracking_norm).where(*sm._exit_check_filter())))

    with session_scope() as db:
        o = db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == "RFEXIT00001"))
        o.left_at = utcnow() - timedelta(days=1)
        o.exit_checked_at = utcnow() - timedelta(hours=sm.EXIT_RECHECK_HOURS + 1)
    assert due()  # still pending: a cancellation / return since the last look must reach it
    with session_scope() as db:
        db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == "RFEXIT00001")).exit_checked_at = utcnow()
    assert not due()


def test_spare_credit_call_gives_up_at_once_on_429(monkeypatch):
    monkeypatch.setattr(client_module, "settings",
                        dataclasses.replace(client_module.settings, oms_token="t", oms_client_id="1"))

    async def go():
        c = OmsClient()
        calls = []

        async def send(method, path, form, params, timeout=None):
            calls.append(path)
            return httpx.Response(429, request=httpx.Request(method, "https://oms.test" + path))

        async def credit(*_a, **_k):
            return None

        monkeypatch.setattr(c, "_send", send)
        monkeypatch.setattr(c, "_wait_for_credit", credit)
        monkeypatch.setattr(c.state, "estimated_remaining", lambda: 60.0)
        try:
            with pytest.raises(OmsThrottled):
                await c.request("POST", "/order_api/invoices", min_credits=30)
        finally:
            await c.aclose()
        return calls

    assert len(asyncio.run(go())) == 1  # no minutes of retrying inside the sync loop


def test_restore_script_sets_the_flag(tmp_path):
    import sqlite3

    import restore_backup

    db = tmp_path / "r.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE sync_state (key VARCHAR(80) PRIMARY KEY, value TEXT, updated_at DATETIME)")
    c.execute("INSERT INTO sync_state VALUES ('trail_audit_progress', '{}', CURRENT_TIMESTAMP)")
    c.commit()
    c.close()
    restore_backup.reset_sync_after_restore(f"sqlite:///{db.as_posix()}")
    c = sqlite3.connect(db)
    rows = dict(c.execute("SELECT key, value FROM sync_state").fetchall())
    c.close()
    assert rows == {"trail_audit_force_deep": "true"}


def test_text_longer_than_its_column_is_cut_not_a_failed_page(eng):
    long_name = "B" * 500
    eng._apply_rows([_row("RFLONG00001", "ODRF7", "ODRF7-1", buyer_name=long_name)], "invoices")
    o = _order("RFLONG00001")
    assert o is not None
    limit = OmsOrder.__table__.c.buyer_name.type.length
    assert limit is None or len(o.buyer_name) <= limit


def test_only_successful_scans_are_counted(eng):
    """User, 9 Oct 2026: "Not found" is flagged and NOT counted as scanned; only successful scans count. The one-time
    "shipped in OMSGuru" marks are not scans by the team either."""
    from app.services import marking
    from app.timeutil import today_dispatch_date

    eng._apply_rows([_row("RFCOUNT0001", "ODRF8", "ODRF8-1")], "invoices")
    packer = _user("rf.counter")
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200

        from app.services import cache

        def dash():
            cache.clear()  # scans below go straight to the service, not through the API that refreshes the cache
            return next(ch for ch in c.get("/api/dashboard").json()["channels"] if ch["id"] == CID)

        def ctx():
            cache.clear()
            return c.get(f"/api/scan-context?channel_id={CID}").json()["stats"]

        d0, x0 = dash(), ctx()
        assert _scan(packer, "RFCOUNT0001")["code"] == "OK"
        assert _scan(packer, "NOTINOMS99887")["code"] == "NOT_IN_OMS"
        d1, x1 = dash(), ctx()
        assert d1["scanned"] == d0["scanned"] + 1 and d1["unverified"] == d0["unverified"] + 1
        assert x1["scanned"] == x0["scanned"] + 1 and x1["not_found"] == x0["not_found"] + 1
        mine = {u["user_id"]: u["scanned"] for u in c.get("/api/dashboard").json()["users"]}
        assert mine[packer] == 1  # the not-found one is not in the packer's count

        # a mark dated today is not a scan
        eng._apply_rows([_row("RFCOUNT0002", "ODRF9", "ODRF9-1", status="Shipped")], "invoices")
        with session_scope() as db:
            db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == "RFCOUNT0002")).shipment_date = utcnow()
            db.flush()
            found = [(r, o) for r, o in marking.candidates(db, None) if r.awb == "RFCOUNT0002"]
            assert found and marking.mark(db, found) == 1
            mk = db.scalar(select(Scan).where(Scan.tracking_norm == "RFCOUNT0002"))
            assert mk.dispatch_date == today_dispatch_date()
        d2 = dash()
        assert d2["scanned"] == d1["scanned"] and d2["marked"] == d1["marked"] + 1
