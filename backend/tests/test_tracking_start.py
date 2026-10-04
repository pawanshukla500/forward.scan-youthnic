"""Admin-set start date: only AWBs generated on or after it count; the clear-data tool starts fresh from it."""
import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS
from app.services import backup, tracking
from app.timeutil import today_dispatch_date

CH = 2  # mock Meesho
CH_ID = int(MOCK_CHANNELS[CH]["id"])
BACKEND = Path(__file__).resolve().parents[1]


def _row(awb: str, age_days: float) -> dict:
    c = MOCK_CHANNELS[CH]
    ts = int(time.time() - age_days * 86400)
    return {"last_id": 4_000_000 + abs(hash(awb)) % 1_000_000, "invoice_id": f"INV/{awb}", "invoice_date": ts,
            "order_date": ts - 3600, "sla_date": ts + 86400, "warehouse": "MAIN", "channel": c["name"],
            "company": c["company_name"], "shipment_tracker": awb, "shipping_company": "Valmo",
            "order_items": [{"channel_order_id": f"OD{awb}", "channel_sub_order_id": f"OD{awb}-1", "sku_code": "TS-SKU",
                             "qty": 1, "invoice_amount": 400, "status": "Ready to ship"}]}


@pytest.fixture(scope="module")
def client():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        asyncio.run(eng._guard("channels", eng.refresh_channels))
        eng._apply_rows([_row("TSOLDAWB0001", 3), _row("TSNEWAWB0001", 0.01)], "orders")
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c
        c.put("/api/admin/tracking-start", json={"date": None})


def _channel(summary: dict) -> dict:
    return next((x for x in summary["channels"] if x["id"] == CH_ID), {"pending": 0, "overdue": 0})


def test_only_awbs_from_the_start_date_count(client):
    before = client.get("/api/reconciliation").json()
    assert before["counted_from"] is None
    overdue_before = {r["awb"] for r in client.get("/api/reconciliation/list", params={"bucket": "overdue"}).json()["rows"]}
    assert "TSOLDAWB0001" in overdue_before

    today = today_dispatch_date().isoformat()
    r = client.put("/api/admin/tracking-start", json={"date": today})
    assert r.status_code == 200 and r.json()["date"] == today
    after = client.get("/api/reconciliation").json()
    assert after["counted_from"] == today
    overdue_after = {r["awb"] for r in client.get("/api/reconciliation/list", params={"bucket": "overdue"}).json()["rows"]}
    assert "TSOLDAWB0001" not in overdue_after and not overdue_after  # nothing from before today is overdue
    pending_today = {r["awb"] for r in client.get("/api/reconciliation/list", params={"bucket": "pending"}).json()["rows"]}
    assert "TSNEWAWB0001" in pending_today
    pend = client.get("/api/pending", params={"channel_id": CH_ID}).json()
    assert {o["tracking"] for o in pend["orders"]} >= {"TSNEWAWB0001"}
    assert "TSOLDAWB0001" not in {o["tracking"] for o in pend["orders"]}
    # an old packet can still be scanned - it is just not counted
    res = client.post("/api/scan", json={"channel_id": CH_ID, "tracking": "TSOLDAWB0001"}).json()
    assert res["severity"] == "success", res


def test_start_date_cannot_be_in_the_future(client):
    tomorrow = (today_dispatch_date() + timedelta(days=1)).isoformat()
    assert client.put("/api/admin/tracking-start", json={"date": tomorrow}).status_code == 400


def test_clear_data_tool_starts_fresh_and_keeps_users(client, tmp_path):
    src = backup.db_path()
    copy = tmp_path / "copy.db"
    s, d = sqlite3.connect(src), sqlite3.connect(copy)
    s.backup(d)
    s.close()
    d.close()
    start = (today_dispatch_date() - timedelta(days=1)).isoformat()
    env = dict(os.environ, DATABASE_URL="sqlite:///" + copy.as_posix(), BACKUP_DIR=(tmp_path / "bk" / "auto").as_posix())
    out = subprocess.run([sys.executable, "clear_data.py", "--yes", "--start", start, "--port", "59998"], cwd=BACKEND,
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    con = sqlite3.connect(copy)
    assert con.execute("SELECT count(*) FROM scans").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM oms_orders").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM users").fetchone()[0] >= 1
    assert con.execute("SELECT count(*) FROM channels").fetchone()[0] >= 6
    assert con.execute("SELECT value FROM sync_state WHERE key='tracking_start'").fetchone()[0] == f'"{start}"'
    con.close()
    saved = list((tmp_path / "bk").glob("before-clear_*/copy.db"))
    assert saved and sqlite3.connect(saved[0]).execute("SELECT count(*) FROM scans").fetchone()[0] > 0
