"""Offline stand-in for OmsClient (OMSGURU_USE_MOCK=true): realistic orders, no API credits used."""
from __future__ import annotations

import random
import time
from typing import Any

from .client import LimiterState

MOCK_CHANNELS = [
    {"id": "18116", "name": "VB EXPORT - Myntra PPMP", "channel_name": "Myntra PPMP", "company_name": "VB EXPORT", "status": "active"},
    {"id": "17040", "name": "ETHNIC JUNCTION - Flipkart", "channel_name": "Flipkart", "company_name": "ETHNIC JUNCTION", "status": "active"},
    {"id": "17970", "name": "Tulip Prints - Meesho", "channel_name": "Meesho", "company_name": "Tulip Prints", "status": "active"},
    {"id": "59585", "name": "VB EXPORT - Ajio B2C Dropship", "channel_name": "Ajio Api", "company_name": "VB EXPORT", "status": "active"},
    {"id": "43245", "name": "VB EXPORT - Amazon - Easyship", "channel_name": "Amazon", "company_name": "VB EXPORT", "status": "active"},
    {"id": "43072", "name": "VB EXPORT - Shopify", "channel_name": "Shopify", "company_name": "VB EXPORT", "status": "active"},
]
_COURIERS = ["Ekart", "Delhivery", "XpressBees", "Shadowfax", "Ecom Express", "Amazon Shipping", "Valmo"]
_CITIES = [("Jaipur", "Rajasthan", "302001"), ("Pune", "Maharashtra", "411001"), ("Lucknow", "Uttar Pradesh", "226001"),
           ("Kochi", "Kerala", "682001"), ("Indore", "Madhya Pradesh", "452001"), ("Guwahati", "Assam", "781001")]
_STATUS_IDS = {1: "Ready to ship", 9: "Packed", 15: "Cancelled", 6: "CancelInit", 19: "Cancelled Before Shipping", 16: "New"}
# How OMSGuru answers order_details(sub_order_id=...) per channel (live API, 2 Oct 2026): Myntra / Cocoblu
# return the exact shipment, Flipkart / Meesho ~50 unrelated rows, Ajio "No order found".
SUB_ORDER_LOOKUP = {"17040": "unrelated", "17970": "unrelated", "59585": "none"}


def mock_awb(i: int) -> str:
    return f"MOCK{100000 + i}"


class MockOmsClient:
    def __init__(self, n_orders: int = 300) -> None:
        self.state = LimiterState(remaining=60, observed_at=time.time())
        self.busy = False  # tests: simulate "no API credit free" for live calls
        rng = random.Random(42)
        now = int(time.time())
        self._orders: list[dict[str, Any]] = []
        for i in range(n_orders):
            ch = MOCK_CHANNELS[i % len(MOCK_CHANNELS)]
            city, state, pin = rng.choice(_CITIES)
            # Mostly ready to ship; every 25th cancelled, every 40th still New.
            status = "Cancelled" if i % 25 == 7 else ("New" if i % 40 == 11 else rng.choice(["Ready to ship", "Packed"]))
            n_items = 1 if rng.random() < 0.85 else 2
            items = [
                {
                    "channel_order_id": f"OD{900000 + i}",
                    "channel_sub_order_id": f"OD{900000 + i}-{k + 1}",
                    "sku_code": rng.choice(["KUR-BLU-M", "KUR-RED-L", "SET-GRN-XL", "DRS-YLW-S", "PLZ-BLK-M"]),
                    "qty": 1,
                    "invoice_amount": rng.choice([499, 649, 799, 999, 1299]),
                    "currency_code": "INR",
                    "status": status,
                }
                for k in range(n_items)
            ]
            ts = now - rng.randint(3600, (6 * 3600) if status == "New" else 5 * 86400)
            self._orders.append(
                {
                    "last_id": 5000000 + i,
                    "invoice_id": f"INV/26-27/{30000 + i}",
                    "invoice_date": ts + 1800,
                    "order_date": ts,
                    "sla_date": ts + 2 * 86400,
                    "warehouse": "MAIN",
                    "channel": ch["name"],
                    "company": ch["company_name"],
                    "buyer_name": rng.choice(["Asha", "Ravi", "Meera", "Kunal", "Sana", "Vikram"]) + " " + rng.choice(["K", "S", "P", "M"]),
                    "buyer_city": city,
                    "buyer_state": state,
                    "buyer_pincode": pin,
                    "shipping_company": rng.choice(_COURIERS),
                    "shipment_tracker": mock_awb(i),
                    "order_type": rng.choice(["COD", "Prepaid"]),
                    "order_items": items,
                    "_status_id": {"Ready to ship": 1, "Packed": 9, "Cancelled": 15, "New": 16}[status],
                }
            )

    async def aclose(self) -> None:
        return None

    def _tick(self) -> None:
        self.state.calls_total += 1
        self.state.last_call_at = time.time()

    async def list_channels(self) -> list[dict]:
        self._tick()
        return MOCK_CHANNELS

    async def list_warehouses(self) -> list[dict]:
        self._tick()
        return [{"id": 1, "name": "Main Warehouse (mock)", "alias": "MAIN"}]

    async def list_channel_listings(self, channel_company_id: int, last_id: int = 0, limit: int | None = None) -> list[dict]:
        self._tick()
        mock_skus = ["KUR-BLU-M", "KUR-RED-L", "SET-GRN-XL", "DRS-YLW-S", "PLZ-BLK-M"]
        rows = [
            {
                "id": 1000 + idx,
                "sku_name": sku,
                "title": f"Ethnic Wear - {sku}",
                "img_url": f"https://cdn.example.com/products/{sku.lower()}.jpg",
            }
            for idx, sku in enumerate(mock_skus)
            if 1000 + idx > last_id
        ]
        return rows[: limit or 100]

    def _page(self, rows: list[dict], last_id: int, limit: int | None) -> list[dict]:
        rows = sorted((r for r in rows if r["last_id"] > last_id), key=lambda r: r["last_id"])
        return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[: limit or 100]]

    async def list_invoices(self, start_ts: int, end_ts: int, last_id: int = 0, limit: int | None = None,
                            live: bool = False, min_credits: int = 0) -> list[dict]:
        self._live_gate(live)
        self._tick()
        # Like OMSGuru: only invoiced orders (Packed onwards) appear here - "New" orders have no invoice / AWB yet.
        rows = [o for o in self._orders if start_ts <= o["invoice_date"] <= end_ts and o["_status_id"] != 16]
        return self._page(rows, last_id, limit)

    async def list_orders(self, start_ts, end_ts, warehouse_id, status_id, last_id=0, limit=None) -> list[dict]:
        self._tick()
        wanted = {15: {15}, 6: {6}, 19: {19}}.get(status_id, {status_id})
        rows = [o for o in self._orders if start_ts <= o["order_date"] <= end_ts and o["_status_id"] in wanted]
        return self._page(rows, last_id, limit)

    async def order_details(self, *, order_id=None, sub_order_id=None, live: bool = False,
                            min_credits: int = 0) -> list[dict]:
        self._live_gate(live)
        self._tick()
        strip = lambda o: {k: v for k, v in o.items() if not k.startswith("_")}  # noqa: E731
        if order_id:
            # Like OMSGuru: an order id returns ONE shipment even when the order has several.
            hits = [o for o in self._orders if o["order_items"][0]["channel_order_id"] == order_id]
            return [strip(hits[0])] if hits else []
        if sub_order_id:
            exact = [o for o in self._orders if any(i["channel_sub_order_id"] == sub_order_id for i in o["order_items"])]
            mode = SUB_ORDER_LOOKUP.get(self._channel_id(exact[0]) if exact else "", "exact")
            unrelated = [o for o in self._orders[:5] if o not in exact]
            if mode == "none":
                return []
            if mode == "unrelated":
                return [strip(o) for o in unrelated]
            # the exact sub-order first, followed by unrelated orders (loose match)
            return [strip(o) for o in exact + unrelated] if exact else []
        return []

    async def order_aging(self, warehouse_id: int, days_ago: int = 0) -> dict:
        self._tick()
        cutoff = time.time() - days_ago * 86400
        by_channel: dict[str, dict[str, Any]] = {}
        for o in self._orders:
            if days_ago and o["order_date"] > cutoff:
                continue
            status = _STATUS_IDS.get(o["_status_id"], "")
            if status not in ("Ready to ship", "Packed", "New"):
                continue
            cid = self._channel_id(o)
            ch = by_channel.setdefault(cid, {"channel_company_id": cid, "channel_company_name": o["channel"],
                                             "orders": 0, "items": 0, "by_status": {}})
            st = ch["by_status"].setdefault(status, {"orders": 0, "items": 0})
            for c in (ch, st):
                c["orders"] += 1
                c["items"] += len(o["order_items"])
        return {"summary": {}, "by_channel": list(by_channel.values())}

    @staticmethod
    def _channel_id(o: dict) -> str:
        return next((c["id"] for c in MOCK_CHANNELS if c["name"] == o["channel"]), "")

    def _live_gate(self, live: bool) -> None:
        if live and self.busy:
            from .client import OmsBusy

            raise OmsBusy("mock: no credit free")

    def set_status(self, i: int, status: str) -> None:
        """Test helper: change an order's status as if someone did it in OMSGuru."""
        o = self._orders[i]
        for it in o["order_items"]:
            it["status"] = status
        o["_status_id"] = {"Ready to ship": 1, "Packed": 9, "Cancelled": 15, "New": 16, "Shipped": 2,
                           "In Transit": 2, "Delivered": 3}.get(status, o["_status_id"])

    def add_order(self, row: dict) -> None:
        """Test helper: an order that exists in OMSGuru but is not in the local copy yet."""
        self._orders.append({"_status_id": 1, **row})
