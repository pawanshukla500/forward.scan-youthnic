"""Restore the Forward Scan database from the automatic backups.

    STOP THE SERVER FIRST (close the run.bat window), then from the project folder:

    .venv\\Scripts\\python backend\\restore_backup.py                 # list the backups
    .venv\\Scripts\\python backend\\restore_backup.py --latest --yes  # newest full backup + newest later recent copy
    .venv\\Scripts\\python backend\\restore_backup.py FILE.db.gz --yes [--recent FILE.db]

What it does: moves the current database AND its -wal / -shm files aside into backups\\incident-<time>\\ (a restored
file next to an old -wal silently replays the wrong pages), unpacks the full backup, merges the newest copy of
recent scans taken after it, and checks the result. Orders re-sync from OMSGuru by themselves after the start;
packets scanned after the recovery point can simply be scanned again (duplicates are still blocked).
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import ROOT_DIR, settings  # noqa: E402
from app.services.backup import RECENT_FULL_TABLES, RECENT_WINDOW_TABLES, db_path  # noqa: E402

BACKUPS = Path(settings.backup_dir)


def _meta(p: Path) -> dict:
    try:
        return json.loads(Path(str(p) + ".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fulls() -> list[Path]:
    files = list((BACKUPS / "daily").glob("fs-*.db.gz")) + list((BACKUPS / "monthly").glob("fs-*.db.gz"))
    return sorted({f.name: f for f in files}.values(), key=lambda f: f.name)


def recents() -> list[Path]:
    return sorted((BACKUPS / "recent").glob("fs-recent-*.db"))


def taken(p: Path) -> str:
    return _meta(p).get("started") or p.name


def apply_recent(restored: Path, recent: Path) -> str:
    """The recent copy is authoritative for its dispatch-date window and for the small tables."""
    c = sqlite3.connect(restored)
    c.isolation_level = None
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("ATTACH DATABASE ? AS r", (str(recent),))
    since = c.execute("SELECT v FROM r._recent_meta WHERE k='since_dispatch_date'").fetchone()[0]
    c.execute("BEGIN IMMEDIATE")
    for t in RECENT_WINDOW_TABLES:
        c.execute(f'DELETE FROM main."{t}" WHERE dispatch_date >= ?', (since,))
        c.execute(f'INSERT OR REPLACE INTO main."{t}" SELECT * FROM r."{t}"')
    for t in RECENT_FULL_TABLES:
        c.execute(f'INSERT OR REPLACE INTO main."{t}" SELECT * FROM r."{t}"')
    # scans whose order is not in this restore keep their own order_json copy
    c.execute("UPDATE scans SET order_id = NULL WHERE order_id IS NOT NULL AND order_id NOT IN (SELECT id FROM oms_orders)")
    c.execute("COMMIT")
    c.execute("DETACH DATABASE r")
    c.close()
    return since


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backup", nargs="?", help="a fs-*.db.gz full backup")
    ap.add_argument("--latest", action="store_true", help="use the newest full backup")
    ap.add_argument("--recent", help="a fs-recent-*.db copy to merge (default: the newest one taken after the backup)")
    ap.add_argument("--no-recent", action="store_true", help="do not merge a recent copy")
    ap.add_argument("--yes", action="store_true", help="really replace the current database")
    ap.add_argument("--force", action="store_true", help="restore a backup taken from a different database file")
    a = ap.parse_args()

    if not a.backup and not a.latest:
        print(f"Backups in {BACKUPS}:")
        for f in fulls():
            m = _meta(f)
            print(f"  full    {f}  taken {m.get('started', '?')}  {m.get('bytes', 0) / 1e6:.1f} MB")
        for f in recents()[-5:]:
            print(f"  recent  {f}  taken {_meta(f).get('started', '?')}")
        print("\nStop the server, then run again with --latest --yes (or a backup file and --yes).")
        return

    full = fulls()[-1] if a.latest else Path(a.backup)
    if not full.exists():
        sys.exit(f"Backup not found: {full}")
    recent = None
    if not a.no_recent:
        recent = Path(a.recent) if a.recent else next((r for r in reversed(recents()) if taken(r) > taken(full)), None)
    target = db_path()
    print(f"Database : {target}\nBackup   : {full} (taken {taken(full)})")
    for f in filter(None, (full, recent)):
        source = _meta(f).get("source")
        if source and Path(source).resolve() != target.resolve() and not a.force:
            sys.exit(f"{f.name} is a backup of {source}, not of {target}. Add --force only if that is really meant.")
    print(f"Recent   : {recent} (taken {taken(recent)})" if recent else "Recent   : none newer than the backup")
    if not a.yes:
        sys.exit("\nNothing changed. Stop the server and add --yes to restore.")

    # Unpack and check the copy first: the current files are only moved once a good restore is ready.
    part = target.with_name(target.name + ".restoring")
    part.unlink(missing_ok=True)
    with gzip.open(full, "rb") as fi, open(part, "wb") as fo:
        shutil.copyfileobj(fi, fo, 4 << 20)
    if recent:
        print(f"Merged recent scans since {apply_recent(part, recent)}")
    c = sqlite3.connect(part)
    check = c.execute("PRAGMA quick_check").fetchone()[0]
    scans = c.execute("SELECT count(*) FROM scans").fetchone()[0]
    c.close()
    if check != "ok":
        part.unlink(missing_ok=True)
        sys.exit(f"The restored copy failed its check ({check}) - nothing was replaced. Try an older backup.")

    incident = ROOT_DIR / "backups" / f"incident-{datetime.now():%Y%m%d-%H%M%S}"
    incident.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        f = Path(str(target) + suffix)
        if f.exists():
            shutil.move(str(f), incident / f.name)
    print(f"Moved the current database files to {incident}")
    part.replace(target)
    print(f"Restored: quick_check ok, {scans:,} scans. Start the server again (run.bat).")


if __name__ == "__main__":
    main()
