from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

# The pool must never run dry: FastAPI runs requests on up to 40 threads and the OMS sync uses its own. With the
# default 5 + 10 connections, 10-15 busy stations used them all and requests and the sync loop waited on each other
# for the whole pool timeout (load test 3 Oct 2026: 30-60 s freezes; with enough connections p99 stayed < 0.3 s).
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=50 if _is_sqlite else 20,
    max_overflow=20,
    pool_timeout=10,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        # WAL lets many scan stations read while one writes.
        cur.execute("PRAGMA journal_mode=WAL")
        # FULL: a power cut cannot lose committed scans (NORMAL could drop the last ~50); ~2 ms per commit.
        cur.execute("PRAGMA synchronous=FULL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA cache_size=-16384")  # 16 MB page cache per connection (default 2 MB)
        cur.execute("PRAGMA journal_size_limit=67108864")  # shrink the -wal file back to 64 MB after a big write
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_columns() -> list[str]:
    """Tiny forward-only migration: add columns that newer code expects to tables that already exist."""
    from sqlalchemy import inspect, text

    added: list[str] = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    default = f" DEFAULT '{val}'" if isinstance(val, str) else f" DEFAULT {int(val)}"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default}'))
                added.append(f"{table.name}.{col.name}")
            for idx in table.indexes:
                if all(c.name in have or f"{table.name}.{c.name}" in added for c in idx.columns):
                    idx.create(conn, checkfirst=True)
    return added


# Single-column indexes replaced by the composite ones in models.py; removed from existing databases at startup.
RETIRED_INDEXES = (
    "ix_scans_day_channel", "ix_scans_dispatch_date", "ix_scans_scanned_at", "ix_scans_user_id", "ix_scans_result",
    "ix_scans_oms_update_status", "ix_scan_events_dispatch_date", "ix_scan_events_outcome",
    "ix_scan_events_created_at", "ix_scan_events_tracking_norm", "ix_oms_orders_channel_id",
    "ix_oms_orders_order_date", "ix_oms_orders_invoice_id",
)


def drop_retired_indexes() -> list[str]:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    have = {ix["name"] for t in insp.get_table_names() for ix in insp.get_indexes(t)}
    gone = [n for n in RETIRED_INDEXES if n in have]
    with engine.begin() as conn:
        for name in gone:
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
    return gone


def optimize(initial: bool = False) -> None:
    """Keep SQLite's query-planner statistics current (without them it picked the wrong indexes)."""
    if not _is_sqlite:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA optimize=0x10002" if initial else "PRAGMA optimize")


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
