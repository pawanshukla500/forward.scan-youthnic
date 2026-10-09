"""Bulk scan: count every still-pending AWB of one sales channel and day as scanned - only on the owner's request
(e.g. packets dispatched without being scanned in the app). They count like manual scans (scan station, dashboard,
reports) under the account "Bulk scan (admin)"; a packer scanning one of these packets later replaces it (OK, never
Duplicate). Every AWB gets an audit event BULK_SCAN.

    python backend/bulk_scan.py --channel "Myntra Youthnic"                     # list - changes nothing
    python backend/bulk_scan.py --channel "Myntra Youthnic" --by "Pawan Shukla" --yes
    python backend/bulk_scan.py --channel 51680 --day 2026-10-09 --by "Pawan Shukla" --yes

Production server:  docker exec -it Forward-Scan python backend/bulk_scan.py --channel ... [--yes]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from app import models  # noqa: E402,F401
from app.db import session_scope  # noqa: E402
from app.models import Channel  # noqa: E402
from app.services import marking  # noqa: E402
from app.timeutil import today_dispatch_date  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", required=True, help="sales channel id or part of its name")
    ap.add_argument("--day", type=date.fromisoformat, default=None, help="AWB day YYYY-MM-DD (default: today)")
    ap.add_argument("--by", default="admin", help="who asked for it (written in every scan's note)")
    ap.add_argument("--yes", action="store_true", help="really record them (default: only list)")
    a = ap.parse_args()
    day = a.day or today_dispatch_date()
    with session_scope() as db:
        q = select(Channel)
        chans = list(db.scalars(q.where(Channel.id == int(a.channel)))) if a.channel.isdigit() else \
            list(db.scalars(q.where(Channel.name.ilike(f"%{a.channel}%"))))
        if len(chans) != 1:
            sys.exit(f"--channel must match exactly one sales channel, matched: {[c.name for c in chans] or 'none'}")
        ch = chans[0]
        recs = marking.bulk_candidates(db, ch.id, day)
        print(f"{ch.name} - AWBs of {day} still pending: {len(recs)}")
        for r in recs[:10]:
            print(f"    {r.awb}")
        if not a.yes:
            print("\nNothing changed. Run again with --yes (and --by \"name\") to count them as scanned.")
            return
        done = marking.bulk_scan(db, recs, day, a.by)
    print(f"Counted {done} AWBs as scanned (Bulk scan by {a.by}).")


if __name__ == "__main__":
    main()
