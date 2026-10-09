"""Put back a scan that was removed (every removed scan is kept in deleted_scans - see models.DeletedScan).

    python backend/restore_deleted_scans.py                       # the last 30 removals - changes nothing
    python backend/restore_deleted_scans.py --awb MYSP1482006653  # removals of one AWB
    python backend/restore_deleted_scans.py --id 12 --yes         # put archived scan 12 back

Production server (inside the app container):
    docker exec -it Forward-Scan python backend/restore_deleted_scans.py [--id N --yes]

A scan is only put back if its AWB has no scan now (otherwise it would be a duplicate). Its batch / order links
are kept when they still exist. An audit event RESTORED is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import desc, select  # noqa: E402

from app import models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import DeletedScan, Manifest, OmsOrder, Scan, User  # noqa: E402
from app.services import cache  # noqa: E402
from app.services.scanning import _event  # noqa: E402
from app.timeutil import to_local  # noqa: E402

def restore(db, archived: DeletedScan, user: User) -> str:
    row = json.loads(archived.data or "{}")
    if db.scalar(select(Scan.id).where(Scan.tracking_norm == archived.tracking_norm)):
        return f"{archived.tracking_norm}: has a scan now - not restored (it would be a duplicate)"
    cols = {c.name: c.type.python_type for c in Scan.__table__.columns}
    vals = {}
    for k, v in row.items():
        if k not in cols or v is None:
            continue
        if cols[k] is datetime:
            v = datetime.fromisoformat(v)
        elif cols[k] is date:
            v = date.fromisoformat(v)
        vals[k] = v
    if vals.get("id") and db.get(Scan, vals["id"]):
        vals.pop("id")
    if vals.get("manifest_id") and not db.get(Manifest, vals["manifest_id"]):
        vals.pop("manifest_id")
    if vals.get("order_id") and not db.get(OmsOrder, vals["order_id"]):
        vals.pop("order_id")
    s = Scan(**vals)
    db.add(s)
    db.flush()
    _event(db, user=user, station="restore", channel_id=s.channel_id, raw=s.tracking_raw, norm=s.tracking_norm,
           outcome="RESTORED", message=f"Removed scan put back (archive #{archived.id}: {archived.reason})"[:300],
           scan_id=s.id)
    return f"{archived.tracking_norm}: restored as scan {s.id}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", type=int, help="the archive id (first column of the listing) to put back")
    ap.add_argument("--awb", help="list the removals of this AWB")
    ap.add_argument("--yes", action="store_true", help="really put it back (default: only list)")
    ap.add_argument("--user", default=settings.admin_username or "admin", help="recorded as who restored it")
    a = ap.parse_args()
    with session_scope() as db:
        q = select(DeletedScan).order_by(desc(DeletedScan.id))
        if a.id:
            q = q.where(DeletedScan.id == a.id)
        elif a.awb:
            q = q.where(DeletedScan.tracking_norm == a.awb.strip().upper())
        rows = list(db.scalars(q.limit(30)))
        for r in rows:
            print(f"{r.id:6}  removed {to_local(r.deleted_at):%d-%b %H:%M}  {r.tracking_norm:20}  scan day {r.dispatch_date}"
                  f"  {r.reason[:80]}")
        if not rows:
            print("Nothing in the archive for that.")
            return
        if not (a.id and a.yes):
            print("\nNothing changed. Run again with --id N --yes to put one back.")
            return
        user = db.scalar(select(User).where(User.username == a.user)) or db.scalar(
            select(User).where(User.role == "admin", User.is_active.is_(True)))
        print(restore(db, rows[0], user))
    cache.clear()


if __name__ == "__main__":
    main()
