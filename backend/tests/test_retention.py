"""Owner, 10 Oct 2026: scanned data is kept at least 2 years; once a month, on the 10th, what was scanned more than
2 years ago is removed - scans, their audit events and the orders they belong to. Every removed scan is written to
backups/removed-scans/ first, so it is out of the database but still on file."""
import asyncio
import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.main import app
from app.models import Channel, OmsOrder, Scan, ScanEvent, User
from app.oms import sync as sm
from app.timeutil import to_local, utcnow

CH = 990004


@pytest.fixture(scope="module", autouse=True)
def _app_started():
    with TestClient(app):  # tables + admin, like a real start
        yield


@pytest.fixture(autouse=True)
def _clean_state():
    for k in ("retention_requested", "retention_tried_at", "retention_last_month", "retention_last"):
        sm._set_state(k, None)
    yield
    for k in ("retention_requested", "retention_tried_at", "retention_last_month", "retention_last"):
        sm._set_state(k, None)


def _at(monkeypatch, utc: datetime):
    monkeypatch.setattr(sm, "utcnow", lambda: utc)


def test_runs_once_a_month_on_the_10th(monkeypatch):
    eng = sm.SyncEngine()
    sm._set_state("retention_last_month", "2028-09")
    _at(monkeypatch, datetime(2028, 10, 9, 20, 0))   # 10 Oct 01:30 IST: before 03:00
    assert not eng._retention_due()
    _at(monkeypatch, datetime(2028, 10, 9, 22, 0))   # 10 Oct 03:30 IST
    assert eng._retention_due()
    assert sm.next_retention_run() == "2028-10-10"
    _at(monkeypatch, datetime(2028, 10, 20, 6, 0))   # the server was off on the 10th: it runs at the first chance
    assert eng._retention_due()
    sm._set_state("retention_last_month", "2028-10")
    assert not eng._retention_due()                   # done for this month
    assert sm.next_retention_run() == "2028-11-10"
    _at(monkeypatch, datetime(2028, 11, 5, 6, 0))
    assert not eng._retention_due()                   # not before the 10th
    sm._set_state("retention_requested", True)        # Admin -> "Run now"
    assert eng._retention_due()


def test_the_monthly_job_removes_what_is_older_than_two_years_and_keeps_a_copy():
    cutoff = sm.retention_cutoff(to_local(utcnow()).date())
    old_day, kept_day = cutoff - timedelta(days=5), cutoff + timedelta(days=5)
    with session_scope() as db:
        if db.get(Channel, CH) is None:
            db.add(Channel(id=CH, name="RET - Test marketplace", marketplace="Ret", scan_enabled=True))
        uid = db.scalar(select(User.id).where(User.username == "admin"))
        for awb, day in (("RETOLD0001", old_day), ("RETKEEP001", kept_day)):
            o = OmsOrder(oms_key=f"ret-{awb}", tracking_raw=awb, tracking_norm=awb, channel_id=CH,
                         channel_label="RET - Test marketplace", channel_order_id=f"OD{awb}", status_group="SHIPPED",
                         awb_generated_at=datetime.combine(day, datetime.min.time()))
            db.add(o)
            db.flush()
            db.add(Scan(tracking_norm=awb, tracking_raw=awb, dispatch_date=day, user_id=uid, channel_id=CH,
                        order_id=o.id, result="OK", message="Verified", order_json=json.dumps({"channel_order_id": f"OD{awb}"})))
            db.add(ScanEvent(dispatch_date=day, user_id=uid, channel_id=CH, tracking_norm=awb, outcome="ACCEPTED"))

    sm.prune_orders()  # the 15-minute clean-up never touches a scanned order, however old
    with session_scope() as db:
        assert db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == "RETOLD0001")) is not None

    eng = sm.SyncEngine()
    asyncio.run(eng._guard("retention", eng.retention))
    assert eng.jobs["retention"].last_ok, eng.jobs["retention"].last_message

    with session_scope() as db:
        assert db.scalar(select(Scan).where(Scan.tracking_norm == "RETOLD0001")) is None
        assert db.scalar(select(ScanEvent).where(ScanEvent.tracking_norm == "RETOLD0001")) is None
        assert db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == "RETOLD0001")) is None
        # inside the 2 years: everything kept
        assert db.scalar(select(Scan).where(Scan.tracking_norm == "RETKEEP001")) is not None
        assert db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == "RETKEEP001")) is not None

    last = sm._get_state("retention_last")
    assert last["scans"] >= 1 and last["kept_from"] == cutoff.isoformat()
    export = Path(settings.backup_dir) / "removed-scans" / last["file"]
    with gzip.open(export, "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    old = next(r for r in rows if r["tracking_norm"] == "RETOLD0001")
    assert old["result"] == "OK" and "ODRETOLD0001" in old["order_json"]
    assert not any(r["tracking_norm"] == "RETKEEP001" for r in rows)
    assert not eng._retention_due()  # done for this month
