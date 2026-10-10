"""Order-trail audit (8 Oct 2026, "a pending number we can trust blindly"): every AWB OMSGuru invoiced today /
yesterday is re-read hourly and compared AWB by AWB with the local copy - missing AWBs are added, statuses refreshed,
and "OMSGuru N = N here" is kept per channel for the Pending page."""
import asyncio
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import session_scope
from app.main import app
from app.models import OmsOrder
from app.oms import sync as sm
from app.oms.mock import MOCK_CHANNELS
from app.timeutil import utcnow

FK = 1  # mock Flipkart channel


def run(coro):
    return asyncio.run(coro)


def audit_round(eng) -> None:
    sm._set_state("trail_audit_progress", None)
    while True:
        run(eng._guard("audit", eng.step_audit))
        assert eng.jobs["audit"].last_ok, eng.jobs["audit"].last_message
        if not sm._get_state("trail_audit_progress"):
            return


def _today_row(awb: str, order_id: str, status: str = "Ready to ship") -> dict:
    c = MOCK_CHANNELS[FK]
    now = int(time.time())
    return {
        "last_id": 6_100_000 + abs(hash(awb)) % 100_000, "invoice_id": f"INV/AUD/{order_id}",
        "invoice_date": now - 300, "order_date": now - 3600, "sla_date": now + 86400, "warehouse": "MAIN",
        "channel": c["name"], "company": c["company_name"], "shipment_tracker": awb, "shipping_company": "Ekart",
        "order_type": "Prepaid", "buyer_name": "Audit Test",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": f"{order_id}-1", "sku_code": "AUD-SKU",
                         "qty": 1, "invoice_amount": 599, "status": status}],
    }


@pytest.fixture(scope="module")
def env():
    eng = sm.SyncEngine()
    sm.engine_instance = eng
    with TestClient(app) as c:
        run(eng._guard("channels", eng.refresh_channels))
        run(eng._guard("invoices", eng.sync_invoices))
        sm._set_state("trail_audit", None)  # results of audits other modules ran
        # Rows earlier test modules inserted by hand are not in the mock OMSGuru's list: the audit would rightly
        # report them as "extra" (test_review_fixes.py checks that) - here they count as seen.
        from app.oms.mapping import normalize_tracking

        known = {normalize_tracking(str(o.get("shipment_tracker") or "")) for o in eng.client._orders}
        with session_scope() as db:
            for o in db.scalars(select(OmsOrder).where(OmsOrder.tracking_norm != "")):
                if o.tracking_norm not in known:
                    o.audit_seen_at = utcnow() + timedelta(days=30)
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c, eng


def _local(awb: str) -> OmsOrder | None:
    with session_scope() as db:
        return db.scalar(select(OmsOrder).where(OmsOrder.tracking_norm == awb))


def test_audit_finds_every_awb_and_adds_the_ones_the_sync_missed(env):
    c, eng = env
    # In OMSGuru, invoiced today, but the incremental sync never saw it (e.g. AWB came after the invoice and the
    # order left Ready-to-ship before the open-order refresh) - and it is already Shipped there.
    eng.client.add_order(_today_row("AUDMISS0001", "ODAUDIT1", "Shipped"))
    assert _local("AUDMISS0001") is None

    audit_round(eng)
    assert _local("AUDMISS0001") is not None  # added -> counted as generated, pending until scanned here
    res = sm._get_state("trail_audit")
    today = max(res["days"])
    t = res["days"][today]
    assert t["complete"] and t["oms"] == t["app"] and t["added"] >= 1, t
    fk = t["channels"][str(int(MOCK_CHANNELS[FK]["id"]))]
    assert fk["oms"] == fk["app"] >= 1

    # The Pending page shows the proof for the day
    summ = c.get("/api/reconciliation").json()
    assert summ["trail"]["complete"] and summ["trail"]["oms"] == summ["trail"]["app"]
    pend = c.get("/api/reconciliation/list", params={"bucket": "pending", "channel_id": int(MOCK_CHANNELS[FK]["id"])}).json()
    assert "AUDMISS0001" in {r["awb"] for r in pend["rows"]}


def test_audit_ignores_marketplace_fulfilment_warehouses(env):
    """Amazon FBA / Flipkart FA orders ship from the marketplace's own warehouse: never pending here."""
    c, eng = env
    row = _today_row("AUDFBA00001", "ODAUDFBA1", "Shipped")
    row["warehouse"] = "DEL4"
    eng.client.add_order(row)
    audit_round(eng)
    assert _local("AUDFBA00001") is None
    t = sm._get_state("trail_audit")["days"]
    assert all(d["complete"] for d in t.values()), {k: (d["oms"], d["app"], d.get("extra_awbs")) for k, d in t.items()}


def test_audit_refreshes_statuses_cancellation_leaves_pending(env):
    c, eng = env
    eng.client.add_order(_today_row("AUDCANC0001", "ODAUDIT2"))
    audit_round(eng)
    assert _local("AUDCANC0001").status_group == "OPEN"
    for o in eng.client._orders:
        if o.get("shipment_tracker") == "AUDCANC0001":
            for it in o["order_items"]:
                it["status"] = "Cancelled"
            o["_status_id"] = 15
    audit_round(eng)
    assert _local("AUDCANC0001").status_group == "CANCELLED"


def test_audit_runs_hourly_on_spare_credits(env):
    _, eng = env
    sm._set_state("trail_audit_progress", None)
    sm._set_state("trail_audit_last_done", time.time())
    assert eng._next_job() != "audit"
    sm._set_state("trail_audit_last_done", time.time() - sm.AUDIT_EVERY_SECONDS - 1)
    # other due jobs (invoices, refresh, ...) come first; once they are done the audit is next
    for key in ("invoices_last_done", "open_orders_last_done", "cancel_sweep_last_done", "cleanup_last_done",
                "channels_last_done", "sku_photos_last_done", "retention_tried_at"):
        sm._set_state(key, time.time())
    sm._set_state("crosscheck_due", False)
    assert eng._next_job() == "audit"
    with session_scope() as db:
        db.execute(delete(OmsOrder).where(OmsOrder.tracking_norm.in_(["AUDMISS0001", "AUDCANC0001"])))


def test_nightly_deep_audit_covers_seven_days_once(env, monkeypatch):
    _, eng = env
    from app.timeutil import today_dispatch_date

    monkeypatch.setattr(sm, "AUDIT_DEEP_HOUR", 0)  # "after 03:00" - any hour in this test
    sm._set_state("trail_audit_deep_on", None)
    sm._set_state("trail_audit", None)
    audit_round(eng)
    days = sm._get_state("trail_audit")["days"]
    assert len(days) == min(sm.AUDIT_DEEP_DAYS, sm.settings.retain_orders_days)  # the deep round
    assert sm._get_state("trail_audit_deep_on") == today_dispatch_date().isoformat()
    sm._set_state("trail_audit", None)
    audit_round(eng)  # later rounds that night / day: today + yesterday again
    assert len(sm._get_state("trail_audit")["days"]) == sm.AUDIT_DAYS


def test_health_reports_the_sync_loop_heartbeat(env):
    c, eng = env
    h = c.get("/api/health").json()
    assert "sync_loop_age_s" in h  # None when sync is off (tests); the watchdog restarts a stalled loop


def test_audit_reports_awbs_here_that_omsguru_does_not_list(env):
    _, eng = env
    row = _today_row("RFXTRA00001", "ODRF5", "Shipped")
    row["invoice_date"] = int(time.time()) - 3600
    eng._apply_rows([row], "invoices")  # here, but not in OMSGuru's list
    sm._set_state("trail_audit", None)
    audit_round(eng)
    days = sm._get_state("trail_audit")["days"]
    hit = [d for d in days.values() if "RFXTRA00001" in d.get("extra_awbs", [])]
    assert hit and hit[0]["extra"] >= 1 and not hit[0]["complete"]
    with session_scope() as db:
        db.execute(delete(OmsOrder).where(OmsOrder.tracking_norm == "RFXTRA00001"))


def test_restore_runs_the_deep_audit_at_once(env, monkeypatch):
    _, eng = env
    from app.timeutil import today_dispatch_date

    monkeypatch.setattr(sm, "AUDIT_DEEP_HOUR", 25)  # the nightly hour never comes in this test
    sm._set_state("trail_audit_deep_on", None)
    sm._set_state("trail_audit_force_deep", True)  # what restore_backup.reset_sync_after_restore leaves
    sm._set_state("trail_audit", None)
    audit_round(eng)
    assert len(sm._get_state("trail_audit")["days"]) == min(sm.AUDIT_DEEP_DAYS, sm.settings.retain_orders_days)
    assert not sm._get_state("trail_audit_force_deep")
    assert sm._get_state("trail_audit_deep_on") == today_dispatch_date().isoformat()


def test_after_an_omsguru_outage_the_sync_catches_up_in_full(env, monkeypatch):
    """9 Oct 2026: OMSGuru was down from 11:43. The new-AWB cursor only moves after a full window, so the first run
    afterwards reads from where it stopped; on top, a deep 7-day order check, a fresh open-order list and a
    cancellation sweep start at once."""
    _, eng = env
    from app.models import SyncLog

    sm._set_state("omsguru_down_since", None)
    cursor_before = sm._get_state("invoices_cursor")

    async def down(job):
        raise sm.OmsDownError("OMSGuru's server is down (HTTP 503)")

    monkeypatch.setattr(eng, "sync_invoices", down)
    run(eng._guard("invoices", eng.sync_invoices))
    assert sm._get_state("omsguru_down_since")
    assert sm._get_state("invoices_cursor") == cursor_before  # a failed run never moves the position
    sm._set_state("omsguru_down_since", time.time() - 3600)  # an hour down
    monkeypatch.undo()
    sm._set_state("open_orders_last_done", time.time())
    sm._set_state("trail_audit_progress", {"windows": [], "last_id": 0})
    run(eng._guard("invoices", eng.sync_invoices))  # OMSGuru answers again
    assert eng.jobs["invoices"].last_ok
    assert sm._get_state("omsguru_down_since") is None
    assert sm._get_state("trail_audit_force_deep") and sm._get_state("trail_audit_progress") is None
    assert sm._get_state("open_orders_last_done") == 0 and sm._get_state("cancel_sweep_last_done") == 0
    with session_scope() as db:
        log = db.scalar(select(SyncLog).where(SyncLog.job == "catch_up").order_by(SyncLog.id.desc()).limit(1))
        assert log is not None and "after 60 min" in log.message
    sm._set_state("trail_audit_force_deep", None)


def test_a_short_blip_does_not_start_a_catch_up(env, monkeypatch):
    _, eng = env
    sm._set_state("omsguru_down_since", time.time() - 30)
    sm._set_state("trail_audit_force_deep", None)
    run(eng._guard("invoices", eng.sync_invoices))
    assert sm._get_state("omsguru_down_since") is None and not sm._get_state("trail_audit_force_deep")



def test_outage_mode_scans_dont_wait_and_only_the_probe_knocks(env, monkeypatch):
    """OMSGuru down (9 Oct 2026, 503 for hours): scans answer from the stored orders at once, only the new-AWB sync
    probes (every DOWN_PROBE_SECONDS), every other job keeps its place; back up -> normal again."""
    _, eng = env
    eng.omsguru_down_since = time.time() - 120
    try:
        sm._set_state("invoices_probe_at", time.time())
        sm._set_state("cleanup_last_done", time.time())
        sm._set_state("retention_tried_at", time.time())  # the monthly clean-up (due on the 10th) is not either
        sm._set_state("open_orders_last_done", 0)  # would be due - but not while OMSGuru is down
        assert eng._next_job() is None
        sm._set_state("invoices_probe_at", time.time() - sm.DOWN_PROBE_SECONDS - 1)
        assert eng._next_job() == "invoices"
        t0 = time.monotonic()
        info = eng.live_lookup("ANYAWB000001", "ANYAWB000001", [])
        assert info["live"] == "down" and time.monotonic() - t0 < 0.1
        assert eng.brief()["omsguru_down_since"] and eng.brief()["invoices_failing"]
    finally:
        eng.omsguru_down_since = None
        sm._set_state("omsguru_down_since", None)
        sm._set_state("open_orders_last_done", time.time())


def test_a_job_stopped_by_a_restart_is_not_recorded_as_a_success(env):
    """9 Oct 2026 12:15:24: a deploy stopped a failing new-AWB sync mid-call and it was logged 'ok' with 0 calls."""
    _, eng = env
    from app.models import SyncLog

    eng.jobs["invoices"].last_ok = False

    async def slow(job):
        await asyncio.sleep(30)

    async def go():
        t = asyncio.create_task(eng._guard("invoices", slow))
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    with session_scope() as db:
        before = db.scalar(select(SyncLog.id).order_by(SyncLog.id.desc()).limit(1)) or 0
    run(go())
    assert eng.jobs["invoices"].last_ok is False and not eng.jobs["invoices"].running
    with session_scope() as db:
        assert db.scalar(select(SyncLog.id).where(SyncLog.id > before, SyncLog.job == "invoices")) is None


def test_omsguru_5xx_is_given_up_after_a_few_tries(monkeypatch):
    import dataclasses

    import httpx

    from app.oms import client as client_module
    from app.oms.client import OmsClient, OmsError

    monkeypatch.setattr(client_module, "settings",
                        dataclasses.replace(client_module.settings, oms_token="t", oms_client_id="1"))
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    async def go():
        c = OmsClient()
        calls = []

        async def send(method, path, form, params, timeout=None):
            calls.append(path)
            return httpx.Response(503, text="<html>503</html>", request=httpx.Request(method, "https://oms.test" + path))

        async def credit(*_a, **_k):
            return None

        monkeypatch.setattr(c, "_send", send)
        monkeypatch.setattr(c, "_wait_for_credit", credit)
        monkeypatch.setattr(c.state, "estimated_remaining", lambda: 60.0)
        monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
        try:
            with pytest.raises(OmsError) as e:
                await c.request("POST", "/order_api/invoices")
        finally:
            await c.aclose()
        return calls, str(e.value)

    calls, err = asyncio.run(go())
    assert len(calls) == client_module.SERVER_ERROR_TRIES and "OMSGuru's server is down" in err
    assert sum(sleeps) < 10  # seconds, not minutes


def test_the_omsguru_connection_monitor_tells_down_key_refused_and_busy_apart(env, monkeypatch):
    """9 Oct 2026: OMSGuru answered 503 (down) from 11:43, then 401 "Invalid API Details" (working, our key refused)."""
    c, eng = env
    for k in ("omsguru_status", "omsguru_incidents", "omsguru_down_since"):
        sm._set_state(k, None)
    monkeypatch.setattr(sm, "DOWN_PROBE_SECONDS", 0)

    def failing(exc):
        async def fn(job):
            raise exc
        return fn

    run(eng._guard("invoices", failing(sm.OmsThrottled("API Throttled"))))
    assert not sm._get_state("omsguru_status") and not eng.omsguru_down_since  # busy is not an outage
    run(eng._guard("invoices", failing(sm.OmsDownError("OMSGuru's server is down (HTTP 503)"))))
    assert sm._get_state("omsguru_status")["state"] == "down" and eng.omsguru_down_since
    run(eng._guard("invoices", failing(sm.OmsAuthError("OMSGuru refused our API key (401: Invalid API Details)"))))
    st = sm._get_state("omsguru_status")
    assert st["state"] == "key_refused" and "Invalid API Details" in st["last_error"]
    assert eng.brief()["omsguru_state"] == "key_refused"
    inc = sm._get_state("omsguru_incidents")
    assert [i["kind"] for i in inc] == ["down", "key_refused"] and inc[0]["end"] and inc[1]["end"] is None
    status = c.get("/api/admin/sync").json()["omsguru"]
    assert status["state"] == "key_refused" and status["incidents"][0]["kind"] == "key_refused"
    sm._set_state("omsguru_down_since", time.time() - 30)  # short: no catch-up needed in this test
    run(eng._guard("invoices", eng.sync_invoices))  # the new token works
    st = sm._get_state("omsguru_status")
    assert st["state"] == "ok" and st["last_ok"] and not eng.omsguru_down_since
    assert sm._get_state("omsguru_incidents")[-1]["end"] is not None
    assert c.get("/api/health").json()["omsguru"] in ("ok", None)

