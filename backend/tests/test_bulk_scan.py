"""Owner, 9 Oct 2026: today's pending Myntra Youthnic AWBs were bulk-marked; "why does it show only 2 successful -
put them all like a manual scan". A bulk scan counts like a manual scan, and a real scan of the packet replaces it."""
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import session_scope
from app.main import app
from app.models import BULK_SCAN_FLAG, Channel, OmsOrder, Scan, User
from app.services import cache, marking, scanning
from app.timeutil import today_dispatch_date, utcnow

CH = 990003


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        with session_scope() as db:
            if db.get(Channel, CH) is None:
                db.add(Channel(id=CH, name="BULK - Test marketplace", marketplace="Bulk", scan_enabled=True))
            for i in range(3):
                db.add(OmsOrder(oms_key=f"bulk-{i}", tracking_raw=f"BULKAWB{i:05d}", tracking_norm=f"BULKAWB{i:05d}",
                                channel_id=CH, channel_label="BULK - Test marketplace", channel_order_id=f"ODBULK{i}",
                                status_group="OPEN", awb_generated_at=utcnow()))
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c


def _ctx(c):
    cache.clear()
    return c.get(f"/api/scan-context?channel_id={CH}").json()


def test_bulk_scan_counts_like_manual_scans(client, monkeypatch):
    import bulk_scan

    before = _ctx(client)
    assert before["awb"]["pending"] == 3 and before["stats"]["scanned"] == 0
    monkeypatch.setattr(sys, "argv", ["bulk_scan.py", "--channel", str(CH), "--by", "Test Owner", "--yes"])
    bulk_scan.main()
    after = _ctx(client)
    assert after["awb"]["pending"] == 0 and after["awb"]["scanned"] == 3
    assert after["stats"]["scanned"] == 3  # successful scans, like manual ones
    cache.clear()
    users = {u["name"]: u["scanned"] for u in client.get("/api/dashboard").json()["users"]}
    assert users.get("Bulk scan (admin)", 0) >= 3
    with session_scope() as db:
        s = db.scalar(select(Scan).where(Scan.tracking_norm == "BULKAWB00000"))
        assert BULK_SCAN_FLAG in s.flags and "Test Owner" in s.message


def test_a_real_scan_of_a_bulk_scanned_packet_is_ok_not_duplicate(client):
    with session_scope() as db:
        packer = db.scalar(select(User).where(User.username == "admin"))
        res = scanning.process_scan(db, user=packer, channel_id=CH, raw="BULKAWB00001", station="T1")
    assert res["severity"] == "success" and res["code"] == "OK", res
    with session_scope() as db:
        rows = db.scalars(select(Scan).where(Scan.tracking_norm == "BULKAWB00001")).all()
        assert len(rows) == 1 and BULK_SCAN_FLAG not in (rows[0].flags or "") and rows[0].station == "T1"
    assert _ctx(client)["stats"]["scanned"] == 3  # replaced, not counted twice
