"""Restore the Forward Scan database from the automatic backups.

    STOP THE SERVER FIRST (close the run.bat window), then from the project folder:

    .venv\\Scripts\\python backend\\restore_backup.py                 # list the backups
    .venv\\Scripts\\python backend\\restore_backup.py --latest --yes  # newest full backup + newest later recent copy
    .venv\\Scripts\\python backend\\restore_backup.py FILE.db.gz --yes [--recent FILE.db]

What it does: moves the current database AND its -wal / -shm files aside into backups\\incident-<time>\\ (a restored
file next to an old -wal silently replays the wrong pages), unpacks the full backup, merges the newest copy of
recent scans taken after it, and checks the result. Orders re-sync from OMSGuru by themselves after the start;
packets scanned after the recovery point can simply be scanned again (duplicates are still blocked).

PostgreSQL (the production server): stop the app container, run the tool in a one-off container, start it again:

    docker stop Forward-Scan
    docker compose -p forward-scan -f docker-compose.production.yml run --rm --no-deps forward-scan \\
        python backend/restore_backup.py --latest --yes
    docker start Forward-Scan

It first saves the current database with pg_dump into data/backups/incident-<time>/, then restores the full
backup (fs-pg-*.dump, pg_restore --clean in one transaction) and merges the newest recent copy taken after it.
A backup downloaded from Google Drive goes into data/backups/pg-<database>/daily/ first (or pass its path).
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import ROOT_DIR, settings  # noqa: E402
from app.services.backup import (  # noqa: E402
    RECENT_FULL_TABLES,
    RECENT_WINDOW_TABLES,
    db_path,
    is_postgres,
    pg_conn_args,
    pg_run,
    verify_pg_dump,
)

BACKUPS = Path(settings.backup_dir)
UPSERT_CHUNK = 1000


def _meta(p: Path) -> dict:
    try:
        return json.loads(Path(str(p) + ".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fulls() -> list[Path]:
    pattern = "fs-pg-*.dump" if is_postgres() else "fs-*.db.gz"
    files = list((BACKUPS / "daily").glob(pattern)) + list((BACKUPS / "monthly").glob(pattern))
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


def apply_recent_postgres(engine, recent: Path) -> str:
    """Merge a recent copy (small SQLite file) into PostgreSQL: authoritative for its dispatch-date window and the
    small tables. Rows keep their ids; links to orders / manifests that are not in this restore are cleared (every
    scan keeps its own order_json copy), and the id sequences are moved past the merged rows."""
    from sqlalchemy import create_engine, delete, select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app import models  # noqa: F401 - registers every table
    from app.db import Base
    from migrate_sqlite_to_postgres import reset_postgres_sequences

    src = create_engine(f"sqlite:///{recent.as_posix()}")
    try:
        with src.connect() as r:
            since = r.exec_driver_sql("SELECT v FROM _recent_meta WHERE k='since_dispatch_date'").scalar_one()
            since_day = date.fromisoformat(since)
            small = [Base.metadata.tables[n] for n in RECENT_FULL_TABLES]
            scans, events = Base.metadata.tables["scans"], Base.metadata.tables["scan_events"]
            with engine.begin() as pg:
                for t in small:  # upsert: rows that other tables point to are never deleted
                    pk = [c.name for c in t.primary_key.columns]
                    rows = [dict(x._mapping) for x in r.execute(select(t))]
                    for i in range(0, len(rows), UPSERT_CHUNK):  # PostgreSQL: max 65,535 parameters a statement
                        stmt = pg_insert(t).values(rows[i:i + UPSERT_CHUNK])
                        stmt = stmt.on_conflict_do_update(
                            index_elements=pk, set_={c.name: stmt.excluded[c.name] for c in t.columns if c.name not in pk})
                        pg.execute(stmt)
                pg.execute(delete(events).where(events.c.dispatch_date >= since_day))
                pg.execute(delete(scans).where(scans.c.dispatch_date >= since_day))
                known_orders = {i for (i,) in pg.execute(select(Base.metadata.tables["oms_orders"].c.id))}
                known_manifests = {i for (i,) in pg.execute(select(Base.metadata.tables["manifests"].c.id))}
                for t in (scans, events):
                    res = r.execute(select(t).order_by(t.c.id))
                    while batch := res.fetchmany(2000):
                        rows = [dict(x._mapping) for x in batch]
                        if t is scans:
                            # The same AWB may still be in the full backup with an older scan that was removed
                            # (voided) afterwards and scanned again - the recent copy is the newer truth.
                            pg.execute(delete(scans).where(scans.c.tracking_norm.in_([x["tracking_norm"] for x in rows])))
                            for row in rows:
                                if row["order_id"] is not None and row["order_id"] not in known_orders:
                                    row["order_id"] = None
                                if row["manifest_id"] is not None and row["manifest_id"] not in known_manifests:
                                    row["manifest_id"] = None
                        pg.execute(t.insert(), rows)
                reset_postgres_sequences(pg, small + [scans, events])
    finally:
        src.dispose()
    return since


def restore_postgres(full: Path, recent: Path | None, url: str | None = None, safety_dump: bool = True) -> dict:
    """Safety pg_dump of the current database, pg_restore --clean of [full], then merge [recent].
    [safety_dump] False: for a database too damaged for pg_dump (--skip-safety-dump)."""
    from sqlalchemy import create_engine, text

    url = url or settings.database_url
    args, env, dbname = pg_conn_args(url)
    check = verify_pg_dump(full)
    if not check["ok"]:
        sys.exit(f"{full.name} cannot be restored: {check['quick_check']} - nothing was changed. Try an older backup.")

    safety = None
    if safety_dump:
        incident = BACKUPS.parent / f"incident-{datetime.now():%Y%m%d-%H%M%S}"
        incident.mkdir(parents=True, exist_ok=True)
        safety = incident / f"before-restore-{dbname}.dump"
        try:
            pg_run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", *args, "-d", dbname, "-f", str(safety)], env)
        except RuntimeError as exc:
            sys.exit(f"Could not save the current database first ({exc}). Nothing was changed. If the database is too "
                     "damaged to be saved, run again with --skip-safety-dump.")
        print(f"Saved the current database to {safety}")

    pg_run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl", "--single-transaction",
            "--exit-on-error", *args, "-d", dbname, str(full)], env)
    print(f"Restored the full backup {full.name}")
    engine = create_engine(url)
    try:
        try:
            since = apply_recent_postgres(engine, recent) if recent else None
        except Exception as exc:  # noqa: BLE001 - say exactly what state the database is in
            sys.exit(f"The full backup IS restored, but merging the recent scans failed: {exc}\n"
                     f"Retry with --recent <another fs-recent-*.db> or --no-recent."
                     + (f" The database before the restore is in {safety}." if safety else ""))
        with engine.connect() as c:
            scans = c.execute(text("SELECT count(*) FROM scans")).scalar_one()
    finally:
        engine.dispose()
    if since:
        print(f"Merged recent scans since {since}")
    print(f"Restored {full.name}: {scans:,} scans. Start the server again (docker start Forward-Scan).")
    return {"scans": scans, "since": since, "safety": str(safety) if safety else None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backup", nargs="?", help="a full backup: fs-*.db.gz (SQLite) or fs-pg-*.dump (PostgreSQL)")
    ap.add_argument("--latest", action="store_true", help="use the newest full backup")
    ap.add_argument("--recent", help="a fs-recent-*.db copy to merge (default: the newest one taken after the backup)")
    ap.add_argument("--no-recent", action="store_true", help="do not merge a recent copy")
    ap.add_argument("--yes", action="store_true", help="really replace the current database")
    ap.add_argument("--force", action="store_true", help="restore a backup taken from a different database file")
    ap.add_argument("--skip-safety-dump", action="store_true",
                    help="PostgreSQL: do not save the current database first (only when it is too damaged for pg_dump)")
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
    if is_postgres():
        _, _, dbname = pg_conn_args()
        print(f"Database : PostgreSQL {dbname}\nBackup   : {full} (taken {taken(full)})")
        print(f"Recent   : {recent} (taken {taken(recent)})" if recent else "Recent   : none newer than the backup")
        for f in filter(None, (full, recent)):
            source = _meta(f).get("source")
            if source and source != dbname and not a.force:
                sys.exit(f"{f.name} is a backup of database {source}, not of {dbname}. Add --force only if that is really meant.")
        if not a.yes:
            sys.exit("\nNothing changed. Stop the app container and add --yes to restore.")
        restore_postgres(full, recent, safety_dump=not a.skip_safety_dump)
        return

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
