"""Is the local copy right? Cross-check with OMSGuru's own pending report, the refresh order, and what
unscanned orders became after they left Packed / Ready-to-ship."""
import asyncio
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.main import app
from app.models import OmsOrder, Scan
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS, mock_awb
from app.services import reconcile

AJIO = 3


def run(coro):
    return asyncio.run(coro)


def _age_previous_refresh():
    """The previous refresh was a while ago (a refresh's start is kept in whole seconds)."""
    with session_scope() as db:
        for o in db.scalars(select(OmsOrder).where(OmsOrder.seen_open_at.isnot(None))):
            o.seen_open_at -= timedelta(minutes=1)


def full_refresh(eng):
    _age_previous_refresh()
    sm._set_state("open_orders_progress", None)
    while True:
        run(eng._guard("open_orders", eng.step_open_orders))
        if not sm._get_state("open_orders_progress"):
            return


def _order(i: int) -> OmsOrder:
    with session_scope() as db:
        return db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == mock_awb(i)))


def _mock_index(eng, status_id: int, channel: int, skip: int = 0) -> int:
    """A mock order of that status and channel that no other test has scanned (the database is shared)."""
    name = MOCK_CHANNELS[channel]["name"]
    with session_scope() as db:
        scanned = set(db.scalars(select(Scan.tracking_norm)))
    hits = [i for i, o in enumerate(eng.client._orders[:300])
            if o["_status_id"] == status_id and o["channel"] == name and mock_awb(i) not in scanned]
    return hits[skip]


@pytest.fixture(scope="module")
def env():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        run(eng._guard("channels", eng.refresh_channels))
        run(eng._guard("invoices", eng.sync_invoices))  # picks the dispatch warehouse
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        full_refresh(eng)
        yield c, eng


def test_crosscheck_agrees_after_a_full_refresh_and_flags_a_gap(env):
    c, eng = env
    assert sm._get_state("crosscheck_due")  # every completed refresh queues a check
    run(eng._guard("crosscheck", eng.crosscheck))
    res = sm._get_state("crosscheck")
    assert res["ok"] and res["oms"] == res["local"] > 0, res
    assert not sm._get_state("crosscheck_due")

    # Five Ready-to-ship Ajio orders exist in OMSGuru that the local copy has not seen yet.
    ch = MOCK_CHANNELS[AJIO]
    now = int(time.time())
    for k in range(5):
        eng.client.add_order({
            "last_id": 6_000_000 + k, "invoice_id": f"INV/GAP{k}", "invoice_date": now - 600, "order_date": now - 3600,
            "warehouse": "MAIN", "channel": ch["name"], "company": ch["company_name"], "shipment_tracker": f"GAPAWB{k:04d}",
            "order_items": [{"channel_order_id": f"GAP{k}", "channel_sub_order_id": f"GAP{k}.1", "qty": 1, "status": "Ready to ship"}],
        })
    run(eng._guard("crosscheck", eng.crosscheck))
    res = sm._get_state("crosscheck")
    ajio = next(r for r in res["channels"] if r["channel_id"] == int(ch["id"]))
    assert not res["ok"] and not ajio["ok"] and ajio["diff"] == -5
    assert res["retry"] and eng.jobs["crosscheck"].last_message.startswith("Differs from OMSGuru")
    # one early re-refresh is scheduled (due within 5 minutes instead of 30)
    assert eng._due("open_orders_last_done", 5 * 60 + 5)

    full_refresh(eng)
    run(eng._guard("crosscheck", eng.crosscheck))
    assert sm._get_state("crosscheck")["ok"]
    assert c.get("/api/admin/sync").json()["crosscheck"]["ok"]


def test_order_moving_from_packed_to_ready_to_ship_mid_refresh_is_not_marked_as_left(env):
    _, eng = env
    i = _mock_index(eng, 9, 0)  # a Packed Myntra order
    _age_previous_refresh()
    sm._set_state("open_orders_progress", None)
    sm._set_state("open_orders_last_done", 0)
    while True:  # page through until the Packed list is done
        run(eng._guard("open_orders", eng.step_open_orders))
        prog = sm._get_state("open_orders_progress")
        if prog["st"] == 1:
            break
    eng.client.set_status(i, "Ready to ship")  # packer marks it RTS while the refresh is running
    while sm._get_state("open_orders_progress"):
        run(eng._guard("open_orders", eng.step_open_orders))
    o = _order(i)
    assert o.status_group == "OPEN" and o.left_at is None


def test_exit_check_finds_out_what_unscanned_orders_became(env):
    c, eng = env
    shipped = _mock_index(eng, 1, 0, skip=3)    # Myntra: OMSGuru finds it by sub-order id
    cancelled = _mock_index(eng, 1, AJIO)       # Ajio: only by order id
    scanned = _mock_index(eng, 1, 0, skip=4)
    assert c.post("/api/scan", json={"channel_id": int(MOCK_CHANNELS[0]["id"]),
                                     "tracking": mock_awb(scanned)}).json()["severity"] == "success"
    for i, status in ((shipped, "In Transit"), (cancelled, "Cancelled"), (scanned, "In Transit")):
        eng.client.set_status(i, status)
    full_refresh(eng)  # none of the three is Packed / Ready-to-ship any more: left, reason unknown
    assert {_order(i).status_group for i in (shipped, cancelled, scanned)} == {"MOVED"}

    for _ in range(100):  # a few orders per run, until nothing is left to look up
        run(eng._guard("exit_check", eng.exit_check))
        if eng.jobs["exit_check"].last_message == "nothing to check":
            break
    assert _order(shipped).status_group == "SHIPPED"
    assert _order(cancelled).status_group == "CANCELLED"
    # the scanned one went out with a scan - nothing to look up
    assert _order(scanned).status_group == "MOVED" and _order(scanned).exit_checked_at is None
    assert c.get("/api/admin/sync").json()["exit_check_pending"] == 0

    with session_scope() as db:
        buckets = {r.awb: r.bucket() for r in reconcile.collect(db)}
    assert buckets[mock_awb(shipped)] == "left_unscanned"     # a real "dispatched without a scan?"
    assert buckets[mock_awb(cancelled)] == "cancelled"        # not a miss: cancelled after the AWB
    assert buckets[mock_awb(scanned)] == "scanned"
    # nothing left to check: the job idles instead of polling
    assert not eng._due("exit_check_idle_at", sm.EXIT_CHECK_IDLE_SECONDS)


def test_scan_station_is_told_when_the_sync_is_failing_or_behind(env):
    c, eng = env
    inv = eng.jobs["invoices"]
    inv.last_ok, inv.last_message = False, "401 Unauthorized - check OMSGURU_API_TOKEN / OMSGURU_CLIENT_ID"
    try:
        brief = c.get("/api/sync/brief").json()
        assert brief["invoices_failing"] and "401" in brief["invoices_error"]
        res = c.post("/api/scan", json={"channel_id": int(MOCK_CHANNELS[0]["id"]), "tracking": "SYNCDOWN0001"}).json()
        assert res["code"] == "NOT_IN_OMS" and "sync is failing" in res["message"], res
    finally:
        inv.last_ok, inv.last_message = True, ""
    assert c.get("/api/sync/brief").json()["invoices_failing"] is False
    assert c.get("/api/sync/brief").json()["last_invoice_sync"]  # when new AWBs last came in
