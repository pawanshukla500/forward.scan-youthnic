"""Fixes from the 3 Oct 2026 database / load review: backups, the first-scan race, safe pruning, the retention floor,
live-call admission and the shared scan-context cache."""
import asyncio
import gzip
import shutil
import sqlite3
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import config
from app.db import SessionLocal, session_scope
from app.main import app
from app.models import Channel, Manifest, Scan, ScanEvent, User
from app.oms import sync as sm
from app.oms.client import OmsBusy, OmsClient
from app.oms.mock import MOCK_CHANNELS
from app.services import backup
from app.services.scanning import process_scan
from app.timeutil import today_dispatch_date, utcnow

CH = int(MOCK_CHANNELS[5]["id"])  # Shopify in the mock


@pytest.fixture(scope="module")
def client():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        asyncio.run(eng._guard("channels", eng.refresh_channels))
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c


def scan(c, tracking, channel=CH):
    r = c.post("/api/scan", json={"channel_id": channel, "tracking": tracking})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_and_recent_backups_restore_every_scan(client, tmp_path):
    for i in range(3):
        assert scan(client, f"BKPAWB{i:05d}")["code"] in ("NOT_IN_OMS", "OK")
    full = backup.run_full()
    assert full["ok"] and full["verify"]["quick_check"] == "ok" and full["verify"]["counts"]["scans"] >= 3
    landed = backup.backup_dir() / "daily" / full["file"]
    assert landed.exists() and (backup.backup_dir() / "monthly" / full["file"]).exists()

    scan(client, "BKPAWB-AFTER")  # scanned after the full backup: only the recent copy has it
    recent = backup.run_recent()
    assert recent["ok"] and recent["counts"]["scans"] >= 4

    from restore_backup import apply_recent  # the restore tool's merge step, on a scratch copy

    restored = tmp_path / "restored.db"
    with gzip.open(landed, "rb") as fi, open(restored, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    apply_recent(restored, backup.backup_dir() / "recent" / recent["file"])
    con = sqlite3.connect(restored)
    awbs = {r[0] for r in con.execute("SELECT tracking_norm FROM scans")}
    assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    con.close()
    assert {"BKPAWB00000", "BKPAWB-AFTER"} <= awbs

    st = client.get("/api/admin/backups").json()
    assert st["full"]["ok"] and st["full_age_hours"] < 0.1 and st["recent"]["ok"]
    assert any("only on this PC" in p for p in st["problems"])  # no BACKUP_MIRROR_DIR set


def test_first_scans_of_the_day_from_several_stations_at_once(client):
    with session_scope() as db:
        uid = db.scalar(select(User.id).where(User.username == "admin"))
        db.merge(Channel(id=990001, name="Race test channel", marketplace="Race", scan_enabled=True))
    failures = []
    for trial in range(8):
        with session_scope() as db:
            db.execute(delete(Scan).where(Scan.channel_id == 990001))
            db.execute(delete(Manifest).where(Manifest.channel_id == 990001))
        gate = threading.Barrier(4)

        def station(k, trial=trial):
            db = SessionLocal()
            try:
                user = db.get(User, uid)
                gate.wait()
                res = process_scan(db, user=user, channel_id=990001, raw=f"RACE{trial:02d}STATION{k}", station=f"S{k}")
                if not res.get("scan"):
                    failures.append(res)
            except Exception as exc:  # noqa: BLE001
                failures.append(repr(exc))
            finally:
                db.close()

        threads = [threading.Thread(target=station, args=(k,)) for k in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert not failures, failures[:3]
    with session_scope() as db:
        assert db.scalar(select(func.count(Manifest.id)).where(Manifest.channel_id == 990001)) == 1


def test_old_scans_are_pruned_in_batches(client, monkeypatch):
    old_day = today_dispatch_date() - timedelta(days=sm.settings.scan_retention_days + 3)
    with session_scope() as db:
        uid = db.scalar(select(User.id))
        m = Manifest(dispatch_date=old_day, channel_id=CH, seq=1)
        db.add(m)
        db.flush()
        db.add_all(Scan(tracking_norm=f"PRUNE{i:05d}", tracking_raw=f"PRUNE{i:05d}", dispatch_date=old_day, user_id=uid,
                        channel_id=CH, manifest_id=m.id) for i in range(120))
        db.add_all(ScanEvent(dispatch_date=old_day, user_id=uid, channel_id=CH, outcome="ACCEPTED") for _ in range(130))
    monkeypatch.setattr(sm, "PRUNE_BATCH", 50)
    assert sm.prune_scans() == 120
    with session_scope() as db:
        assert db.scalar(select(func.count(Scan.id)).where(Scan.dispatch_date == old_day)) == 0
        assert db.scalar(select(func.count(ScanEvent.id)).where(ScanEvent.dispatch_date == old_day)) == 0
        assert db.scalar(select(func.count(Manifest.id)).where(Manifest.dispatch_date == old_day)) == 0


@pytest.mark.parametrize("value,force,expected", [("60", "", 365), ("0", "", 0), ("400", "", 400), ("60", "true", 60)])
def test_scan_retention_under_a_year_needs_force(monkeypatch, value, force, expected):
    monkeypatch.setenv("SCAN_RETENTION_DAYS", value)
    monkeypatch.setenv("SCAN_RETENTION_FORCE", force)
    assert config._scan_retention_days() == expected


@pytest.mark.parametrize("value,force,expected", [("60", "", 365), ("0", "", 0), ("550", "", 550), ("60", "true", 60)])
def test_scanned_orders_retention_under_a_year_needs_force(monkeypatch, value, force, expected):
    monkeypatch.setenv("SCANNED_ORDERS_RETENTION_DAYS", value)
    monkeypatch.setenv("SCAN_RETENTION_FORCE", force)
    assert config._scanned_orders_retention_days() == expected


def test_live_calls_leave_credits_for_sync_and_never_queue():
    async def go():
        cl = OmsClient()
        sent = []

        async def fake_send(*a, **k):
            sent.append(a)
            raise AssertionError("must not reach OMSGuru")

        cl._send = fake_send
        try:
            cl.state.remaining, cl.state.observed_at = 8, time.time()  # below what background sync needs + 2
            with pytest.raises(OmsBusy):
                await cl._live_request("POST", "/order_api/order_details", form={}, params=None)
            cl.state.remaining = 60
            await cl._lock.acquire(priority=1)  # a background call is running
            t0 = time.monotonic()
            with pytest.raises(OmsBusy):
                await cl._live_request("POST", "/order_api/order_details", form={}, params=None)
            assert time.monotonic() - t0 < 1.5
            cl._lock.release()
            assert not cl._lock._locked and not sent
        finally:
            await cl.aclose()

    asyncio.run(go())


def test_scan_context_is_shared_and_shows_a_new_scan(client):
    url = f"/api/scan-context?channel_id={CH}"
    first = client.get(url).json()
    before = first["stats"]["scanned"]
    again = client.get(url).json()
    assert again["server_time"] == first["server_time"]  # served from the shared answer
    saved = scan(client, "CTXCACHE00001")["scan"]
    # (in production an answer is recomputed at most once per CACHE_MIN_INTERVAL; tests run with 0)
    after = client.get(url).json()
    assert after["stats"]["scanned"] == before + 1 and after["server_time"] > saved["scanned_at"]


def test_cache_computes_once_for_simultaneous_requests():
    from app.services import cache

    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
        return {"n": len(calls)}

    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.cached("sf-test", 42, 5.0, slow))) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1 and results == [{"n": 1}] * 8
    cache.invalidate(42)  # stale now, but recomputed at most once per min_interval
    assert cache.cached("sf-test", 42, 5.0, slow, min_interval=10) == {"n": 1} and len(calls) == 1
    assert cache.cached("sf-test", 42, 5.0, slow) == {"n": 2}
