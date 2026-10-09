"""Remove saved "Not found" (UNVERIFIED) scans that were not AWBs at all - another barcode on the label or packet
(Myntra packet id MPP3EM..., the 2-D route code 5|\\MB-...|O|NAG/WRA|..., product EAN codes, courier bag ids).
Since 8 Oct 2026 the scan screen rejects these as "WRONG BARCODE"; this cleans up the ones saved before.

    python backend/cleanup_wrong_barcodes.py                 # list what would be removed - changes nothing
    python backend/cleanup_wrong_barcodes.py --yes           # remove them
    python backend/cleanup_wrong_barcodes.py --days 7 --yes  # only scans of the last 7 dispatch days

Production server (inside the app container, which already has the database settings):
    docker exec -it Forward-Scan python backend/cleanup_wrong_barcodes.py [--yes]

Uses the same rule as the scan screen (services/awb_shapes.py): a scan is removed only when OMSGuru still does not
know the code AND it has symbols or does not look like any AWB of its sales channel. "Not found" scans that do look
like a real AWB are kept - they turn OK by themselves if the order ever syncs. Every removal is written to the audit
trail (scan_events, outcome VOIDED) like a supervisor's "remove scan", so it can be traced.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from app import models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Channel, Manifest, Scan, User  # noqa: E402
from app.services import awb_shapes, cache  # noqa: E402
from app.services.scanning import _event, find_orders  # noqa: E402
from app.timeutil import today_dispatch_date  # noqa: E402


def candidates(db, days: int | None, closed: list | None = None, every: bool = False) -> list[tuple[Scan, str]]:
    """Wrong-barcode Not-found scans that may be removed. Those in a CLOSED manifest (dispatch already handed over;
    only an admin removes scans there in the app) are left alone and put in [closed] for the listing."""
    channels = {c.id: c.name for c in db.scalars(select(Channel))}
    closed_ids = set(db.scalars(select(Manifest.id).where(Manifest.status == "CLOSED")))
    q = select(Scan).where(Scan.result == "UNVERIFIED")
    if days:
        q = q.where(Scan.dispatch_date >= today_dispatch_date() - timedelta(days=days - 1))
    out = []
    for s in db.scalars(q.order_by(Scan.id)).unique():
        if find_orders(db, s.tracking_norm):
            continue  # OMSGuru knows it now - reverify turns it OK / Check on the next sync
        why = awb_shapes.wrong_barcode(db, s.channel_id, s.tracking_raw, s.tracking_norm, channels.get(s.channel_id, ""))
        if not why and every:
            why = "Not found in OMSGuru: only verified scans are kept (rule of 9 Oct 2026)"
        if not why:
            continue
        if s.manifest_id in closed_ids:
            if closed is not None:
                closed.append(s)
            continue
        out.append((s, why))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="really remove them (default: only list)")
    ap.add_argument("--days", type=int, default=None, help="only scans of the last N dispatch days")
    ap.add_argument("--all", action="store_true",
                    help="every saved 'Not found' scan OMSGuru still does not know, also AWB-shaped ones (since 9 Oct "
                         "2026 Not found is never saved)")
    ap.add_argument("--user", default=settings.admin_username or "admin", help="recorded as who removed them")
    a = ap.parse_args()

    with session_scope() as db:
        closed: list = []
        found = candidates(db, a.days, closed, every=a.all)
        by_channel = Counter(s.channel.name if s.channel else "?" for s, _ in found)
        kept = db.query(Scan).filter(Scan.result == "UNVERIFIED").count() - len(found)
        print(f"Wrong-barcode 'Not found' scans: {len(found)}  (other 'Not found' scans kept: {kept})")
        for name, n in by_channel.most_common():
            print(f"  {n:5}  {name}")
        for s, _ in found[:15]:
            print(f"    {s.dispatch_date}  {s.tracking_raw[:50]}")
        if closed:
            print(f"Also {len(closed)} in CLOSED manifests - left alone (remove them in the app as admin if needed):")
            for s in closed[:10]:
                print(f"    {s.dispatch_date}  {s.tracking_raw[:50]}")
        if not a.yes:
            print("\nNothing changed. Run again with --yes to remove them.")
            return
        user = db.scalar(select(User).where(User.username == a.user)) or db.scalar(
            select(User).where(User.role == "admin", User.is_active.is_(True)))
        if user is None:
            sys.exit("No admin account to record the removal under - pass --user <username>")
        for s, why in found:
            _event(db, user=user, station="cleanup", channel_id=s.channel_id, raw=s.tracking_raw, norm=s.tracking_norm,
                   outcome="VOIDED", message=f"Removed by cleanup: {why}" if a.all else
                   f"Removed by cleanup: not an AWB (wrong barcode). {why}", scan_id=s.id)
            db.info["delete_reason"] = f"Removed by cleanup: {why}"
            db.delete(s)
    cache.clear()
    print(f"Removed {len(found)} scans (audit trail: scan_events, outcome VOIDED).")


if __name__ == "__main__":
    main()
