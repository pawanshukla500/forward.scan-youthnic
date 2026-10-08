"""One-time: mark the AWBs that are still pending here but OMSGuru already shows Shipped / In Transit as scanned,
dated on the day OMSGuru shipped them. After that only real scans take AWBs out of Pending (see services/marking.py).

    python backend/mark_shipped_backlog.py                       # list per ship day and channel - changes nothing
    python backend/mark_shipped_backlog.py --yes                 # mark them
    python backend/mark_shipped_backlog.py --until 2026-10-08 --yes  # only those shipped on or before that day

Production server (inside the app container):
    docker exec -it Forward-Scan python backend/mark_shipped_backlog.py [--yes]

Marked AWBs are recorded under "OMSGuru (marked shipped)" (sign-in disabled), station "OMSGuru shipped", with an
audit event each; a real scan of one of them later replaces the mark and is OK (never "Duplicate").
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import models  # noqa: E402,F401
from app.db import session_scope  # noqa: E402
from app.services import marking  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="really mark them (default: only list)")
    ap.add_argument("--until", type=date.fromisoformat, default=None,
                    help="only AWBs OMSGuru shipped on or before this day (YYYY-MM-DD)")
    a = ap.parse_args()

    with session_scope() as db:
        found = marking.candidates(db, a.until)
        print(f"Pending here but already SHIPPED in OMSGuru: {len(found)} AWBs")
        for (day, channel), n in sorted(marking.summarize(db, found).items()):
            print(f"  shipped {day}  {n:5}  {channel}")
        if not a.yes:
            print("\nNothing changed. Run again with --yes to mark them as scanned on their ship date.")
            return
        done = marking.mark(db, found)
    print(f"Marked {done} AWBs as scanned on the day OMSGuru shipped them.")


if __name__ == "__main__":
    main()
