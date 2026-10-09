"""Owner, 9 Oct 2026: "scanned data - make sure that by any change we can never lose it from the database".

Every scan the app removes is copied whole to deleted_scans first (models.DeletedScan) and can be put back
(restore_deleted_scans.py); and no code may drop tables, empty the scans table or bulk-delete scans outside the
3-year retention clean-up - this test fails the build if a change tries."""
import json
import re

import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.main import app
from app.models import Channel, DeletedScan, OmsOrder, Scan, ScanEvent, User
from app.timeutil import today_dispatch_date, utcnow

BACKEND = Path(__file__).resolve().parents[1]


ORDER_JSON = '{"channel_order_id": "ODLOSS", "items": [{"sku": "LOSS-SKU", "qty": 1}]}'


@pytest.fixture(autouse=True, scope="module")
def _app_started():
    with TestClient(app):  # creates the tables (and the deleted_scans one) like a real start
        yield
    with session_scope() as db:  # leave no test scans behind for later modules
        db.query(Scan).filter(Scan.tracking_norm.like("LOSSAWB%")).delete(synchronize_session=False)
        db.query(OmsOrder).filter(OmsOrder.tracking_norm.like("LOSSAWB%")).delete(synchronize_session=False)


def _scan_row(awb: str) -> int:
    with session_scope() as db:
        ch = db.scalar(select(Channel).where(Channel.scan_enabled.is_(True)))
        if ch is None:
            ch = Channel(id=990002, name="LOSS - Test", marketplace="Test")
            db.add(ch)
            db.flush()
        u = db.scalar(select(User).where(User.username == "admin"))
        db.add(OmsOrder(oms_key=f"loss-{awb}", tracking_raw=awb, tracking_norm=awb, channel_id=ch.id, channel_label=ch.name,
                        channel_order_id=f"OD{awb}", status_group="OPEN", awb_generated_at=utcnow()))
        s = Scan(tracking_norm=awb, tracking_raw=awb, dispatch_date=today_dispatch_date(), scanned_at=utcnow(),
                 user_id=u.id, channel_id=ch.id, result="OK", message="Verified", order_json=ORDER_JSON)
        db.add(s)
        db.flush()
        return s.id


def test_a_removed_scan_is_kept_whole_and_can_be_put_back(monkeypatch):
    import restore_backup  # noqa: F401 - the backend folder is importable
    import restore_deleted_scans

    sid = _scan_row("LOSSAWB00001")
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        assert c.delete(f"/api/scans/{sid}", params={"reason": "packed twice"}).status_code == 200
    with session_scope() as db:
        assert db.get(Scan, sid) is None
        arch = db.scalar(select(DeletedScan).where(DeletedScan.scan_id == sid))
        assert arch is not None and "packed twice" in arch.reason
        row = json.loads(arch.data)
        assert row["tracking_norm"] == "LOSSAWB00001" and row["order_json"] == ORDER_JSON and row["result"] == "OK"
        aid = arch.id
    monkeypatch.setattr("sys.argv", ["restore_deleted_scans.py", "--id", str(aid), "--yes"])
    restore_deleted_scans.main()
    with session_scope() as db:
        back = db.scalar(select(Scan).where(Scan.tracking_norm == "LOSSAWB00001"))
        assert back is not None and back.result == "OK" and back.order_json == ORDER_JSON
        assert db.scalar(select(ScanEvent).where(ScanEvent.tracking_norm == "LOSSAWB00001", ScanEvent.outcome == "RESTORED"))
    # putting it back twice would make a duplicate: refused
    monkeypatch.setattr("sys.argv", ["restore_deleted_scans.py", "--id", str(aid), "--yes"])
    restore_deleted_scans.main()
    with session_scope() as db:
        assert len(db.scalars(select(Scan).where(Scan.tracking_norm == "LOSSAWB00001")).all()) == 1


def test_any_orm_delete_of_a_scan_is_archived():
    sid = _scan_row("LOSSAWB00002")
    with session_scope() as db:
        db.delete(db.get(Scan, sid))  # no reason given: still archived
    with session_scope() as db:
        arch = db.scalar(select(DeletedScan).where(DeletedScan.scan_id == sid))
        assert arch is not None and arch.reason == "removed"


def test_a_rolled_back_delete_leaves_no_archive_row():
    sid = _scan_row("LOSSAWB00003")
    with session_scope() as db:
        db.delete(db.get(Scan, sid))
        db.flush()
        db.rollback()
    with session_scope() as db:
        assert db.get(Scan, sid) is not None
        # (by AWB: SQLite hands a freed row id out again)
        assert db.scalar(select(DeletedScan).where(DeletedScan.tracking_norm == "LOSSAWB00003")) is None


# "TRUNCATE <table>" only: SQLite's wal_checkpoint(TRUNCATE) and the word "truncated" are no data loss
DESTRUCTIVE = re.compile(r"drop_all\(|DROP\s+TABLE|TRUNCATE\s+(TABLE\s+)?\"?\w|DROP\s+SCHEMA|DELETE\s+FROM\s+\"?scans\b",
                         re.I)
BULK_SCAN_DELETE = re.compile(r"delete\(\s*Scan\s*\)|Scan\.__table__\.delete\(|model\.__table__\.delete\("
                              r"|query\(\s*Scan\s*\)[^\n]*\.delete\(")


def test_no_code_can_wipe_or_bulk_delete_scans():
    """Guard for every future change: destructive SQL anywhere in the app is refused; a bulk delete of scans is only
    allowed in the retention clean-up (sync.prune_scans). clear_data.py (manual 'start fresh' on the local SQLite
    file, saves a checked copy first) is the one deliberate exception."""
    files = list((BACKEND / "app").rglob("*.py")) + [p for p in BACKEND.glob("*.py") if p.name != "clear_data.py"]
    bad = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        for m in DESTRUCTIVE.finditer(text):
            bad.append(f"{f.name}: {m.group(0)}")
        for m in BULK_SCAN_DELETE.finditer(text):
            fn = text[: m.start()].rsplit("\ndef ", 1)[-1].split("(", 1)[0]
            if not (f.name == "sync.py" and fn == "prune_scans"):
                bad.append(f"{f.name} in {fn}(): {m.group(0)}")
    assert not bad, "code that could lose scanned data:\n" + "\n".join(bad)
