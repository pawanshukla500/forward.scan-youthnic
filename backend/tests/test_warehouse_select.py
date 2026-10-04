from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import OmsOrder, Warehouse
from app.oms import sync as sm


def test_auto_select_syncs_only_the_dispatch_warehouse():
    # Mirrors the live account: one own warehouse + marketplace fulfilment centres that hold a handful of orders.
    with TestClient(app):
        with session_scope() as db:
            db.query(Warehouse).delete()
            db.add_all([
                Warehouse(id=6688, name="DEFAULT 33299", sync_enabled=True),
                Warehouse(id=11955, name="BLR5", sync_enabled=True),
                Warehouse(id=11956, name="BOM7", sync_enabled=True),
            ])
            for i in range(990):
                db.add(OmsOrder(oms_key=f"wh-test-{i}", warehouse="DEFAULT 33299"))
            for i in range(4):
                db.add(OmsOrder(oms_key=f"wh-fba-{i}", warehouse="BLR5"))
        sm._set_state("warehouses_mode", None)
        eng = sm.SyncEngine()

        picked = eng._auto_select_warehouses()
        assert picked and picked.startswith("DEFAULT 33299")
        assert eng._warehouse_ids() == [6688]

        # Once an admin changes warehouses by hand, auto-select leaves them alone.
        sm._set_state("warehouses_mode", "manual")
        with session_scope() as db:
            db.get(Warehouse, 11955).sync_enabled = True
        assert eng._auto_select_warehouses() is None
        assert eng._warehouse_ids() == [6688, 11955]


def test_fallback_to_default_warehouse_when_no_data():
    with TestClient(app):
        with session_scope() as db:
            db.query(OmsOrder).filter(OmsOrder.oms_key.like("wh-%")).delete(synchronize_session=False)
            db.query(Warehouse).delete()
            db.add_all([Warehouse(id=1, name="DEFAULT 999", sync_enabled=False), Warehouse(id=2, name="DEL4", sync_enabled=False)])
        sm._set_state("warehouses_mode", None)
        eng = sm.SyncEngine()
        # Existing mock orders say "MAIN", which matches neither warehouse -> fall back to DEFAULT.
        assert eng._auto_select_warehouses() == "DEFAULT 999"
        assert eng._warehouse_ids() == [1]
        assert sm._get_state("warehouses_mode") is None  # not final; re-evaluated after the next sync
