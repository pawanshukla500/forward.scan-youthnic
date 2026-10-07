import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.config import normalize_database_url
from app.db import Base
from migrate_sqlite_to_postgres import (
    clean_row_for_table,
    compute_sha256,
    migrate,
    normalize_postgres_url,
    reset_postgres_sequences,
    verify_tables_and_data,
)


def test_url_normalization():
    assert normalize_database_url("postgres://u:p@host:5432/db") == "postgresql+psycopg://u:p@host:5432/db"
    assert normalize_database_url("postgresql://u:p@host:5432/db") == "postgresql+psycopg://u:p@host:5432/db"
    assert normalize_database_url("postgresql+psycopg://u:p@host:5432/db") == "postgresql+psycopg://u:p@host:5432/db"
    assert normalize_database_url("sqlite:///data/test.db") == "sqlite:///data/test.db"

    assert normalize_postgres_url("postgres://u:p@host:5432/db") == "postgresql+psycopg://u:p@host:5432/db"
    assert normalize_postgres_url("postgresql://u:p@host:5432/db") == "postgresql+psycopg://u:p@host:5432/db"


def test_clean_row_conversion():
    user_table = Base.metadata.tables["users"]
    raw_user_row = {
        "id": 1,
        "username": "test_user",
        "full_name": "Test User",
        "email": "test@example.com",
        "password_hash": "hash123",
        "role": "scanner",
        "is_active": 1,  # SQLite integer boolean
        "token_version": 0,
        "must_change_password": 0,  # SQLite integer boolean
        "password_changed_at": None,
        "created_at": "2026-10-07T12:00:00Z",  # SQLite string datetime
        "last_login_at": None,
    }
    cleaned = clean_row_for_table(raw_user_row, user_table)
    assert cleaned["is_active"] is True
    assert cleaned["must_change_password"] is False
    assert isinstance(cleaned["created_at"], datetime)


def test_sha256_computation(tmp_path):
    p = tmp_path / "sample.txt"
    p.write_bytes(b"Forward Scan Warehouse")
    digest = compute_sha256(p)
    assert len(digest) == 64
    assert isinstance(digest, str)


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL environment variable is not configured",
)
def test_full_migration_to_postgres(tmp_path):
    pg_url = normalize_postgres_url(os.environ["TEST_POSTGRES_URL"])
    pg_engine = create_engine(pg_url)

    # 1. Clean public schema on test PG
    with pg_engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        conn.commit()

    # 2. Prepare sample SQLite DB
    sqlite_db_path = tmp_path / "source.db"
    sqlite_conn = sqlite3.connect(sqlite_db_path)
    sqlite_cur = sqlite_conn.cursor()

    sqlite_cur.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT DEFAULT '',
            email TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'scanner',
            is_active BOOLEAN DEFAULT 1,
            token_version INTEGER DEFAULT 0,
            must_change_password BOOLEAN DEFAULT 0,
            password_changed_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL,
            last_login_at TIMESTAMP
        );
    """)
    sqlite_cur.execute("""
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            code TEXT UNIQUE NOT NULL,
            marketplace TEXT NOT NULL,
            company TEXT NOT NULL,
            color TEXT NOT NULL,
            scan_prefix TEXT DEFAULT '',
            scan_enabled BOOLEAN DEFAULT 1,
            sort_order INTEGER DEFAULT 10,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        );
    """)
    sqlite_cur.execute("""
        CREATE TABLE scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL REFERENCES channels(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            manifest_id INTEGER,
            order_id INTEGER,
            tracking_raw TEXT NOT NULL,
            tracking_norm TEXT NOT NULL,
            result TEXT NOT NULL,
            message TEXT DEFAULT '',
            flags_json TEXT DEFAULT '[]',
            station TEXT DEFAULT '',
            device_info TEXT DEFAULT '',
            client_ip TEXT DEFAULT '',
            sync_attempted BOOLEAN DEFAULT 0,
            sync_succeeded BOOLEAN DEFAULT 0,
            sync_error TEXT DEFAULT '',
            scanned_at TIMESTAMP NOT NULL
        );
    """)
    sqlite_cur.execute("""
        CREATE TABLE scan_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER REFERENCES scans(id),
            channel_id INTEGER NOT NULL REFERENCES channels(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            tracking_raw TEXT NOT NULL,
            tracking_norm TEXT NOT NULL,
            station TEXT DEFAULT '',
            outcome TEXT NOT NULL,
            reason TEXT DEFAULT '',
            meta_json TEXT DEFAULT '{}',
            scanned_at TIMESTAMP NOT NULL
        );
    """)

    sqlite_cur.execute("""
        INSERT INTO users (id, username, full_name, email, password_hash, role, is_active, created_at)
        VALUES (1, 'pawan.shukla', 'Pawan Shukla', 'test@test.com', 'bcrypt_hash', 'admin', 1, '2026-10-07 10:00:00');
    """)
    sqlite_cur.execute("""
        INSERT INTO channels (id, name, code, marketplace, company, color, scan_enabled, sort_order, created_at, updated_at)
        VALUES (1, 'Flipkart PPMP', 'FK_PPMP', 'Flipkart PPMP', 'VB EXPORT', '#126B4E', 1, 1, '2026-10-07 10:00:00', '2026-10-07 10:00:00');
    """)
    sqlite_cur.execute("""
        INSERT INTO scans (id, channel_id, user_id, tracking_raw, tracking_norm, result, scanned_at)
        VALUES (1, 1, 1, 'FMPC0001', 'FMPC0001', 'OK', '2026-10-07 10:30:00');
    """)
    sqlite_cur.execute("""
        INSERT INTO scan_events (id, scan_id, channel_id, user_id, tracking_raw, tracking_norm, outcome, scanned_at)
        VALUES (1, 1, 1, 1, 'FMPC0001', 'FMPC0001', 'OK', '2026-10-07 10:30:00');
    """)
    sqlite_conn.commit()
    sqlite_conn.close()

    # 3. Execute migration
    report = migrate(
        source_path=sqlite_db_path,
        target_url=pg_url,
        dry_run=False,
        verify_only=False,
        force=False,
        report_path=tmp_path / "migration_report.json",
    )

    assert report["success"] is True
    assert (tmp_path / "migration_report.json").exists()

    # 4. Verify sequences reset and new insert succeeds
    with pg_engine.connect() as conn:
        res = conn.execute(text("INSERT INTO users (username, password_hash, created_at) VALUES ('new_user', 'hash', NOW()) RETURNING id;")).fetchone()
        assert res[0] == 2
