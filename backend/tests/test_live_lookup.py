"""Every scan fetches the order from OMSGuru live (order_details by order id), with safe fallbacks."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.oms import sync as sync_module
from app.oms.mock import MOCK_CHANNELS, mock_awb


def _ch(i: int) -> int:
    return int(MOCK_CHANNELS[i % len(MOCK_CHANNELS)]["id"])


def _new_order(awb: str, order_id: str, channel_idx: int, invoice_ts: int, status: str = "Ready to ship",
               sub: str = "") -> dict:
    ch = MOCK_CHANNELS[channel_idx]
    return {
        "last_id": 9_000_000 + int(time.time() * 1000) % 1_000_000, "invoice_id": f"INV/{order_id}",
        "invoice_date": invoice_ts, "order_date": invoice_ts - 600, "sla_date": invoice_ts + 86400,
        "warehouse": "MAIN", "channel": ch["name"], "company": ch["company_name"], "buyer_name": "Live Test",
        "buyer_city": "Pune", "buyer_state": "MH", "buyer_pincode": "411001", "shipping_company": "Delhivery",
        "shipment_tracker": awb, "order_type": "Prepaid",
        "order_items": [{"channel_order_id": order_id, "channel_sub_order_id": sub or f"{order_id}-1", "sku_code": "LIVE-SKU",
                         "qty": 1, "invoice_amount": 799, "currency_code": "INR", "status": status}],
    }


@pytest.fixture(scope="module")
def env():
    eng = sync_module.SyncEngine()
    sync_module.engine_instance = eng

    async def seed():
        for name, fn in (("channels", eng.refresh_channels), ("invoices", eng.sync_invoices),
                         ("cancel_sweep", eng.cancel_sweep)):
            await eng._guard(name, fn)
        while True:
            await eng._guard("open_orders", eng.step_open_orders)
            if not sync_module._get_state("open_orders_progress"):
                break

    with TestClient(app) as c:
        asyncio.run(seed())
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c, eng


def scan(c, channel_id, tracking):
    r = c.post("/api/scan", json={"channel_id": channel_id, "tracking": tracking})
    assert r.status_code == 200, r.text
    return r.json()


def test_cached_order_is_refreshed_live(env):
    c, eng = env
    i = 60  # Myntra: OMSGuru finds it by sub-order id
    calls_before = eng.client.state.calls_total
    res = scan(c, _ch(i), mock_awb(i))
    assert res["severity"] == "success" and res["live"] == "fresh"
    assert eng.client.state.calls_total == calls_before + 1  # exactly one order_details call


@pytest.mark.parametrize("first,second", [(61, 73), (74, 80), (75, 81)], ids=["flipkart", "meesho", "ajio"])
def test_channels_without_sub_order_lookup_are_fetched_by_order_id(env, first, second):
    """Live API: Flipkart / Meesho answer a sub-order id with unrelated rows, Ajio with "No order found"."""
    c, eng = env
    before = eng.client.state.calls_total
    res = scan(c, _ch(first), mock_awb(first))
    assert res["severity"] == "success" and res["live"] == "fresh", res
    assert eng.client.state.calls_total - before <= 2
    # learnt: the next scan of that channel goes straight to the order id
    before = eng.client.state.calls_total
    res = scan(c, _ch(second), mock_awb(second))
    assert res["live"] == "fresh" and eng.client.state.calls_total == before + 1
    assert eng._lookup_pref[_ch(second)] == "order"


def test_cancelled_in_oms_after_last_sync_is_blocked_at_scan(env):
    c, eng = env
    i = 67
    eng.client.set_status(i, "Cancelled")  # changed in OMSGuru; local copy still says Ready to ship
    res = scan(c, _ch(i), mock_awb(i))
    assert res["severity"] == "error" and res["code"] == "CANCELLED" and res["live"] == "fresh"


def test_brand_new_invoice_is_found_on_the_spot(env):
    c, eng = env
    eng.client.add_order(_new_order("LIVEAWB0001", "ODLIVE1", 0, int(time.time()) - 30))
    res = scan(c, _ch(0), "LIVEAWB0001")
    assert res["severity"] == "success", res
    assert res["order"]["channel_order_id"] == "ODLIVE1" and res["live"] == "fresh"


def test_order_id_barcode_links_an_unverified_awb(env):
    c, eng = env
    # Invoiced long ago, so neither the local copy nor the newest-invoices pull has it.
    eng.client.add_order(_new_order("OLDAWB00042", "ODOLD42", 1, int(time.time()) - 20 * 86400))
    first = scan(c, _ch(1), "OLDAWB00042")
    assert first["code"] == "NOT_IN_OMS" and "ORDER ID" in first["message"]
    linked = scan(c, _ch(1), "ODOLD42")
    assert linked["severity"] == "success" and linked["code"] == "LINKED", linked
    assert linked["scan"]["id"] == first["scan"]["id"] and linked["scan"]["result"] == "OK"
    # and scanning either barcode again is now a duplicate
    assert scan(c, _ch(1), "ODOLD42")["code"] == "DUPLICATE"


def test_busy_api_falls_back_to_local_copy(env):
    c, eng = env
    i = 71
    eng.client.busy = True
    try:
        res = scan(c, _ch(i), mock_awb(i))
    finally:
        eng.client.busy = False
    assert res["severity"] == "success" and res["live"] == "busy"


def test_duplicates_cost_no_api_call(env):
    c, eng = env
    i = 61  # scanned in the Flipkart test above
    before = eng.client.state.calls_total
    assert scan(c, _ch(i), mock_awb(i))["code"] == "DUPLICATE"
    assert eng.client.state.calls_total == before


@pytest.mark.parametrize("ch,live", [(0, "fresh"), (1, "unconfirmed")], ids=["myntra", "flipkart"])
def test_order_with_two_shipments_uses_the_open_one(env, ch, live):
    """Real case from the live account: OD338671385154646100 (Flipkart) has a returned shipment and a ready one.
    Myntra finds the exact shipment by sub-order id. On Flipkart OMSGuru only answers by order id, with ONE
    shipment - the returned one here - so the scan is judged on the local copy ("unconfirmed")."""
    c, eng = env
    now = int(time.time())
    oid = f"ODMULTI{ch}"
    returned = _new_order(f"MULTIRET{ch}01", oid, ch, now - 5 * 86400, status="Return Init", sub=f"{oid}-1")
    ready = _new_order(f"MULTIRTS{ch}01", oid, ch, now - 3600, sub=f"{oid}-2")
    for row in (returned, ready):
        eng.client.add_order(row)
    eng._apply_rows([returned, ready], "invoices")

    by_order_id = scan(c, _ch(ch), oid)
    assert by_order_id["severity"] == "success", by_order_id
    assert by_order_id["scan"]["tracking_norm"] == f"MULTIRTS{ch}01" and by_order_id["live"] == live
    assert scan(c, _ch(ch), f"MULTIRTS{ch}01")["code"] == "DUPLICATE"
    # the returned shipment was never Packed / Ready-to-ship here, so it is not kept - and never shows OK
    assert scan(c, _ch(ch), f"MULTIRET{ch}01")["severity"] != "success"


def test_order_id_with_two_open_shipments_asks_for_awb(env):
    c, eng = env
    now = int(time.time())
    a = _new_order("AMBAWB0001", "ODAMB0001", 3, now - 3600, sub="ODAMB0001-1")
    b = _new_order("AMBAWB0002", "ODAMB0001", 3, now - 3500, sub="ODAMB0001-2")
    eng._apply_rows([a, b], "invoices")
    res = scan(c, _ch(3), "ODAMB0001")
    assert res["severity"] == "error" and res["code"] == "AMBIGUOUS"
    assert scan(c, _ch(3), "AMBAWB0002")["severity"] == "success"
