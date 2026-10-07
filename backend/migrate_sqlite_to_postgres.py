"""First-class migration utility: SQLite -> PostgreSQL for Forward Scan.

Safely migrates all production tables, rows, primary keys, relationships,
dates, nulls, booleans, and JSON content from SQLite into PostgreSQL with
topological dependency order, transaction safety, automatic sequence repair,
and multi-layer verification.

Usage:
    export DATABASE_URL="postgresql+psycopg://user:pass@host:port/forward_scan?sslmode=disable"
    python backend/migrate_sqlite_to_postgres.py --source /path/to/sqlite.db --target-from-env DATABASE_URL --dry-run
    python backend/migrate_sqlite_to_postgres.py --source /path/to/sqlite.db --target-from-env DATABASE_URL
    python backend/migrate_sqlite_to_postgres.py --source /path/to/sqlite.db --target-from-env DATABASE_URL --verify-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, create_engine, text
from sqlalchemy.engine import make_url

# Ensure backend directory is in path
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import Base
from app import models  # noqa: F401 - register all declarative models

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("migrate")


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_sqlite_integrity(source_path: Path) -> None:
    log.info("Checking SQLite source integrity (%s)...", source_path)
    uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        cur = conn.cursor()
        res = cur.execute("PRAGMA quick_check;").fetchone()
        if not res or res[0] != "ok":
            raise RuntimeError(f"SQLite PRAGMA quick_check failed: {res}")
        log.info("SQLite integrity check PASSED: ok")
    finally:
        conn.close()


def normalize_postgres_url(raw_url: str) -> str:
    if raw_url.startswith("postgres://"):
        return "postgresql+psycopg://" + raw_url[len("postgres://"):]
    if raw_url.startswith("postgresql://") and not raw_url.startswith("postgresql+psycopg://"):
        return "postgresql+psycopg://" + raw_url[len("postgresql://"):]
    return raw_url


def parse_datetime_val(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        # Handle ISO with or without Z or microseconds
        val = val.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(val, fmt)
                except ValueError:
                    pass
    return None


def parse_date_val(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        try:
            return date.fromisoformat(val[:10])
        except ValueError:
            pass
    return None


def clean_row_for_table(row_dict: dict[str, Any], table) -> dict[str, Any]:
    cleaned = {}
    for col in table.columns:
        val = row_dict.get(col.name)
        if val is None:
            cleaned[col.name] = None
        elif isinstance(col.type, Boolean):
            cleaned[col.name] = bool(val)
        elif isinstance(col.type, DateTime):
            cleaned[col.name] = parse_datetime_val(val)
        elif isinstance(col.type, Date):
            cleaned[col.name] = parse_date_val(val)
        else:
            cleaned[col.name] = val
    return cleaned


def reset_postgres_sequences(conn, tables) -> list[dict[str, Any]]:
    results = []
    for table in tables:
        has_int_id = False
        for col in table.columns:
            if col.name == "id" and col.primary_key and getattr(col.type, "python_type", None) is int:
                has_int_id = True
                break

        if not has_int_id:
            continue

        seq_row = conn.execute(
            text(f"SELECT pg_get_serial_sequence('\"{table.name}\"', 'id')")
        ).fetchone()
        seq_name = seq_row[0] if seq_row else None

        if seq_name:
            max_id_row = conn.execute(text(f'SELECT COALESCE(MAX(id), 0) FROM "{table.name}"')).fetchone()
            max_id = max_id_row[0] if max_id_row else 0
            if max_id > 0:
                conn.execute(text(f"SELECT setval('{seq_name}', {max_id}, true)"))
            else:
                conn.execute(text(f"SELECT setval('{seq_name}', 1, false)"))
            log.info("Reset sequence %s for table %s to max_id=%d", seq_name, table.name, max_id)
            results.append({"table": table.name, "sequence": seq_name, "max_id": max_id, "status": "RESET"})
        else:
            results.append({"table": table.name, "sequence": None, "status": "NO_SEQUENCE"})
    return results


def verify_tables_and_data(sqlite_conn: sqlite3.Connection, pg_conn, tables) -> dict[str, Any]:
    log.info("Verifying migration between SQLite source and PostgreSQL target...")
    table_reports = {}
    mismatches = []

    src_tables = set(r[0] for r in sqlite_conn.cursor().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    for table in tables:
        t_name = table.name
        src_cur = sqlite_conn.cursor()
        if t_name in src_tables:
            src_count = src_cur.execute(f'SELECT count(*) FROM "{t_name}"').fetchone()[0]
        else:
            src_count = 0
        pg_count = pg_conn.execute(text(f'SELECT count(*) FROM "{t_name}"')).fetchone()[0]

        has_id = any(c.name == "id" for c in table.columns)
        src_min_id, src_max_id, pg_min_id, pg_max_id = None, None, None, None
        if has_id and src_count > 0:
            src_min_id, src_max_id = src_cur.execute(f'SELECT min(id), max(id) FROM "{t_name}"').fetchone()
            pg_min_id, pg_max_id = pg_conn.execute(text(f'SELECT min(id), max(id) FROM "{t_name}"')).fetchone()

        match = (src_count == pg_count)
        if has_id and src_count > 0:
            match = match and (src_min_id == pg_min_id) and (src_max_id == pg_max_id)

        rep = {
            "source_count": src_count,
            "target_count": pg_count,
            "count_match": (src_count == pg_count),
            "has_id": has_id,
            "source_min_id": src_min_id,
            "source_max_id": src_max_id,
            "target_min_id": pg_min_id,
            "target_max_id": pg_max_id,
            "match": match,
        }
        table_reports[t_name] = rep

        if not match:
            mismatches.append(f"{t_name}: src={src_count} vs pg={pg_count}")
        else:
            log.info("Table '%s': count=%d OK (min=%s, max=%s)", t_name, pg_count, pg_min_id, pg_max_id)

    # Detailed verification for scans
    business_checks = {}
    if "scans" in [t.name for t in tables] and "scans" in src_tables:
        log.info("Checking scans result breakdown...")
        src_results = dict(sqlite_conn.cursor().execute("SELECT result, count(*) FROM scans GROUP BY result").fetchall())
        pg_results = dict(pg_conn.execute(text("SELECT result, count(*) FROM scans GROUP BY result")).fetchall())
        business_checks["scans_by_result"] = {
            "source": src_results,
            "target": pg_results,
            "match": (src_results == pg_results),
        }
        if src_results != pg_results:
            mismatches.append(f"scans_by_result mismatch: {src_results} != {pg_results}")

    # Detailed verification for scan_events
    if "scan_events" in [t.name for t in tables] and "scan_events" in src_tables:
        log.info("Checking scan_events outcome breakdown...")
        src_outcomes = dict(sqlite_conn.cursor().execute("SELECT outcome, count(*) FROM scan_events GROUP BY outcome").fetchall())
        pg_outcomes = dict(pg_conn.execute(text("SELECT outcome, count(*) FROM scan_events GROUP BY outcome")).fetchall())
        business_checks["scan_events_by_outcome"] = {
            "source": src_outcomes,
            "target": pg_outcomes,
            "match": (src_outcomes == pg_outcomes),
        }
        if src_outcomes != pg_outcomes:
            mismatches.append(f"scan_events_by_outcome mismatch: {src_outcomes} != {pg_outcomes}")

    # Integrity of foreign key relationships in target PostgreSQL
    log.info("Verifying FK integrity in PostgreSQL target...")
    orphaned_scans_user = pg_conn.execute(
        text("SELECT count(*) FROM scans s LEFT JOIN users u ON s.user_id = u.id WHERE u.id IS NULL")
    ).fetchone()[0]
    orphaned_scans_channel = pg_conn.execute(
        text("SELECT count(*) FROM scans s LEFT JOIN channels c ON s.channel_id = c.id WHERE c.id IS NULL")
    ).fetchone()[0]
    orphaned_scans_order = pg_conn.execute(
        text("SELECT count(*) FROM scans s LEFT JOIN oms_orders o ON s.order_id = o.id WHERE s.order_id IS NOT NULL AND o.id IS NULL")
    ).fetchone()[0]

    business_checks["fk_integrity"] = {
        "orphaned_scans_user": orphaned_scans_user,
        "orphaned_scans_channel": orphaned_scans_channel,
        "orphaned_scans_order": orphaned_scans_order,
        "clean": (orphaned_scans_user == 0 and orphaned_scans_channel == 0 and orphaned_scans_order == 0),
    }

    if orphaned_scans_user > 0 or orphaned_scans_channel > 0 or orphaned_scans_order > 0:
        mismatches.append(f"FK violations found in scans: user={orphaned_scans_user}, channel={orphaned_scans_channel}, order={orphaned_scans_order}")

    return {
        "all_matched": len(mismatches) == 0,
        "mismatches": mismatches,
        "tables": table_reports,
        "business_checks": business_checks,
    }


def migrate(
    source_path: Path,
    target_url: str,
    dry_run: bool = False,
    verify_only: bool = False,
    force: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    start_time = datetime.now()

    if not source_path.exists():
        raise FileNotFoundError(f"Source SQLite database not found: {source_path}")

    source_sha256 = compute_sha256(source_path)
    log.info("Source SQLite: %s (SHA256: %s)", source_path, source_sha256)
    check_sqlite_integrity(source_path)

    clean_pg_url = normalize_postgres_url(target_url)
    u = make_url(clean_pg_url)
    log.info("Connecting to target PostgreSQL: database=%s, user=%s, host=%s, port=%s",
             u.database, u.username, u.host, u.port)

    pg_engine = create_engine(clean_pg_url, pool_pre_ping=True)

    # 1. Inspect destination database
    with pg_engine.connect() as conn:
        dest_meta = conn.execute(text("SELECT current_database(), current_user, version();")).fetchone()
        log.info("Destination connected: DB='%s', User='%s', Engine='%s'",
                 dest_meta[0], dest_meta[1], dest_meta[2][:50])

    sqlite_uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
    sqlite_conn = sqlite3.connect(sqlite_uri, uri=True, timeout=60)
    sqlite_conn.row_factory = sqlite3.Row

    # All declarative models in dependency order
    sorted_tables = Base.metadata.sorted_tables
    log.info("Discovered %d models in Base.metadata dependency order:", len(sorted_tables))
    for idx, t in enumerate(sorted_tables, 1):
        log.info("  [%02d] %s", idx, t.name)

    # Ensure schema exists on target
    if not verify_only:
        log.info("Ensuring PostgreSQL schema exists via Base.metadata.create_all...")
        Base.metadata.create_all(pg_engine)

    # Check existing data in target
    with pg_engine.connect() as conn:
        existing_rows = 0
        for t in sorted_tables:
            try:
                cnt = conn.execute(text(f'SELECT count(*) FROM "{t.name}"')).fetchone()[0]
                existing_rows += cnt
            except Exception:
                pass
        if existing_rows > 0 and not verify_only and not force:
            raise RuntimeError(
                f"Destination database already contains {existing_rows} rows across tables. "
                f"Aborting migration for safety. Use --verify-only or --force."
            )

    sequence_results = []
    verification_results = {}

    if verify_only:
        log.info("Running in --verify-only mode...")
        with pg_engine.connect() as conn:
            verification_results = verify_tables_and_data(sqlite_conn, conn, sorted_tables)
    else:
        log.info("Beginning data migration (dry_run=%s)...", dry_run)
        src_tables = set(r[0] for r in sqlite_conn.cursor().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        # Migrate inside transaction
        with pg_engine.connect() as conn:
            trans = conn.begin()
            try:
                for table in sorted_tables:
                    t_name = table.name
                    if t_name not in src_tables:
                        log.info("Table '%s' not present in source SQLite; skipping data copy (target table created).", t_name)
                        continue

                    cur = sqlite_conn.cursor()
                    src_count = cur.execute(f'SELECT count(*) FROM "{t_name}"').fetchone()[0]
                    log.info("Migrating table '%s' (%d rows)...", t_name, src_count)

                    if src_count == 0:
                        continue

                    cur.execute(f'SELECT * FROM "{t_name}"')
                    col_names = [d[0] for d in cur.description]

                    chunk_size = 1000
                    inserted = 0
                    while True:
                        rows = cur.fetchmany(chunk_size)
                        if not rows:
                            break
                        chunk_dicts = []
                        for r in rows:
                            row_dict = dict(zip(col_names, r))
                            cleaned = clean_row_for_table(row_dict, table)
                            chunk_dicts.append(cleaned)

                        conn.execute(table.insert(), chunk_dicts)
                        inserted += len(chunk_dicts)

                    log.info("  -> Table '%s' migrated (%d/%d rows)", t_name, inserted, src_count)

                # Reset sequences
                log.info("Resetting PostgreSQL sequences...")
                sequence_results = reset_postgres_sequences(conn, sorted_tables)

                # Verify inside transaction
                verification_results = verify_tables_and_data(sqlite_conn, conn, sorted_tables)
                if not verification_results["all_matched"]:
                    raise RuntimeError(f"Verification failed: {verification_results['mismatches']}")

                if dry_run:
                    log.info("Dry run requested: rolling back transaction.")
                    trans.rollback()
                else:
                    log.info("All checks passed: committing transaction to PostgreSQL!")
                    trans.commit()

            except Exception:
                log.exception("Migration failed! Rolling back destination transaction.")
                trans.rollback()
                raise

    sqlite_conn.close()

    duration = round(time.perf_counter() - t0, 2)
    report = {
        "migration_timestamp": start_time.isoformat(timespec="seconds"),
        "duration_seconds": duration,
        "dry_run": dry_run,
        "verify_only": verify_only,
        "source_sqlite_path": str(source_path),
        "source_sqlite_sha256": source_sha256,
        "target_database": u.database,
        "target_user": u.username,
        "target_host": u.host,
        "target_port": u.port,
        "status": "SUCCESS" if verification_results.get("all_matched", False) else "FAILED",
        "sequences": sequence_results,
        "verification": verification_results,
    }

    if report_path:
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        log.info("Migration report written to %s", report_path)

    log.info("Migration finished in %.2fs with status: %s", duration, report["status"])
    return report


def main():
    parser = argparse.ArgumentParser(description="Migrate Forward Scan SQLite database to PostgreSQL")
    parser.add_argument("--source", required=True, help="Path to source SQLite .db file")
    parser.add_argument("--target-from-env", default="DATABASE_URL",
                        help="Environment variable containing PostgreSQL connection string (default: DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate migration and rollback")
    parser.add_argument("--verify-only", action="store_true", help="Verify source and target without modifying target")
    parser.add_argument("--force", action="store_true", help="Allow migration into non-empty target")
    parser.add_argument("--report", default="migration-report.json", help="Path for migration-report.json output")
    args = parser.parse_args()

    target_url = os.environ.get(args.target_from_env)
    if not target_url:
        print(f"Error: Environment variable '{args.target_from_env}' is not set or empty.", file=sys.stderr)
        sys.exit(1)

    source_path = Path(args.source)
    report_path = Path(args.report)

    try:
        rep = migrate(
            source_path=source_path,
            target_url=target_url,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
            force=args.force,
            report_path=report_path,
        )
        if rep["status"] != "SUCCESS":
            sys.exit(1)
    except Exception as exc:
        log.error("Migration halted: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
