"""Start fresh: empty the order and scan data and count orders from a new date.

    STOP THE SERVER FIRST (close the run.bat window), then from the project folder:

    .venv\\Scripts\\python backend\\clear_data.py --yes                     # count from today
    .venv\\Scripts\\python backend\\clear_data.py --yes --start 2026-10-03

Removed: OMSGuru orders, scans, the scan audit log, manifests, sync history and sync progress (OMSGuru orders are
downloaded again on the next start). Kept: users and passwords, sales channels (names, colours, scan on/off),
warehouses (which ones sync) and settings in .env. A complete, checked copy of the database is saved to
backups\\before-clear_<time>\\ first; it is never deleted automatically.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402
from app.services.backup import db_path  # noqa: E402
from app.timeutil import today_dispatch_date, utcnow  # noqa: E402

CLEARED = ("scans", "scan_events", "manifests", "oms_orders", "sync_logs")
KEEP_STATE = ("warehouses_mode",)  # an admin's warehouse choice survives


def server_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="count orders from this day (default: today)")
    ap.add_argument("--yes", action="store_true", help="really empty the data")
    ap.add_argument("--port", type=int, default=8000, help="port the server runs on (checked to be stopped)")
    a = ap.parse_args()

    if not settings.database_url.startswith("sqlite:///"):
        sys.exit("This tool handles the SQLite database only.")
    db = db_path()
    start = a.start or today_dispatch_date()
    if start > today_dispatch_date():
        sys.exit("The start date cannot be in the future.")
    con = sqlite3.connect(db)
    counts = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in CLEARED}
    con.close()
    print(f"Database : {db}")
    print("Will empty: " + ", ".join(f"{t} ({n:,})" for t, n in counts.items()))
    print(f"Orders will count from {start:%d %b %Y}")
    if not a.yes:
        sys.exit("\nNothing changed. Stop the server and add --yes.")
    if server_running(a.port):
        sys.exit(f"The server is still running on port {a.port} - close the run.bat window first.")

    # 1. a complete, checked copy first
    keep = Path(settings.backup_dir).parent / f"before-clear_{datetime.now():%Y-%m-%d_%H%M%S}"
    keep.mkdir(parents=True, exist_ok=True)
    copy = keep / db.name
    src, dst = sqlite3.connect(db), sqlite3.connect(copy)
    try:
        src.backup(dst, pages=-1)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        src.close()
        dst.close()
    chk = sqlite3.connect(copy)
    ok = chk.execute("PRAGMA quick_check").fetchone()[0]
    saved = chk.execute("SELECT count(*) FROM scans").fetchone()[0]
    chk.close()
    if ok != "ok":
        sys.exit(f"The safety copy failed its check ({ok}) - nothing was cleared.")
    print(f"Saved a checked copy ({saved:,} scans) to {copy}")

    # 2. empty the data in one transaction, keep users / channels / warehouses
    con = sqlite3.connect(db)
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("BEGIN IMMEDIATE")
    for t in CLEARED:  # children before parents: scans -> manifests / oms_orders
        con.execute(f'DELETE FROM "{t}"')
    marks = ",".join("?" * len(KEEP_STATE))
    con.execute(f"DELETE FROM sync_state WHERE key NOT IN ({marks})", KEEP_STATE)
    con.execute("INSERT OR REPLACE INTO sync_state (key, value, updated_at) VALUES ('tracking_start', ?, ?)",
                (json.dumps(start.isoformat()), utcnow().isoformat(sep=" ")))
    con.execute("COMMIT")
    con.execute("VACUUM")  # give the space back
    con.close()
    print(f"Cleared. Orders count from {start:%d %b %Y}. Start the server again (run.bat) - it downloads the "
          "open orders from OMSGuru by itself.")


if __name__ == "__main__":
    main()
