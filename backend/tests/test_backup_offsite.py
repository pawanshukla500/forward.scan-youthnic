"""Backups 8 Oct 2026: offsite copy (rclone / Google Drive), PostgreSQL backups on the data volume, PostgreSQL
recent copies that no longer wipe the daily history, and a PostgreSQL backup -> restore round trip."""
import dataclasses
import hashlib
import json
import os
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.services import backup


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).status_code == 200
        yield c


def fake_rclone(cloud: Path, corrupt: bool = False):
    """Stands in for the rclone binary: "remote:path" lives under [cloud]; md5 like Google Drive reports it."""

    def local(remote: str) -> Path:
        return cloud / remote.split(":", 1)[1]

    def run(args, timeout=900):
        cmd = args[0]
        if cmd == "copyto":
            dst = local(args[-1])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args[-2], dst)
            if corrupt:
                dst.write_bytes(b"truncated")
            return ""
        if cmd == "lsjson":
            f = local(args[-1])
            if not f.exists():
                return "[]"
            return json.dumps([{"Path": f.name, "Size": f.stat().st_size,
                                "Hashes": {"md5": hashlib.md5(f.read_bytes()).hexdigest()}}])
        if cmd == "delete":
            return ""
        raise AssertionError(f"unexpected rclone {cmd}")

    return run


def with_settings(monkeypatch, **changes):
    monkeypatch.setattr(backup, "settings", dataclasses.replace(backup.settings, **changes))


def test_offsite_copy_is_uploaded_checked_and_reported(client, tmp_path, monkeypatch):
    cloud = tmp_path / "cloud"
    # its own folder: the other backup tests expect to make this month's first (monthly) backup
    with_settings(monkeypatch, backup_offsite_remote="fake:Forward Scan Backups", backup_dir=str(tmp_path / "bk"))
    monkeypatch.setattr(backup, "_rclone", fake_rclone(cloud))

    full = backup.run_full()
    assert full["ok"] and full["offsite"]["ok"] and full["offsite"]["checked"] == "size + md5"
    in_cloud = cloud / "Forward Scan Backups" / backup.backup_dir().name / "daily" / full["file"]
    assert in_cloud.read_bytes() == (backup.backup_dir() / "daily" / full["file"]).read_bytes()

    recent = backup.run_recent()
    assert recent["offsite"]["ok"]
    assert backup.run_recent()["offsite"]["at"] == recent["offsite"]["at"]  # uploaded at most hourly

    st = client.get("/api/admin/backups").json()
    assert st["offsite"]["remote"] == "fake:Forward Scan Backups" and st["offsite"]["full"]["ok"]
    assert not any("only on this" in p for p in st["problems"]), st["problems"]  # "Protected", not "At risk"

    # a copy that does not arrive intact is a red problem, not a silent success
    monkeypatch.setattr(backup, "_rclone", fake_rclone(cloud, corrupt=True))
    bad = backup.run_full()
    assert bad["ok"] and bad["offsite"]["ok"] is False
    assert any("Offsite copy of the full backup failed" in p for p in client.get("/api/admin/backups").json()["problems"])


def test_offsite_remote_comes_from_the_rclone_config_on_the_server(tmp_path, monkeypatch):
    conf = tmp_path / "rclone.conf"
    with_settings(monkeypatch, backup_offsite_remote="", backup_rclone_config=str(conf))
    assert backup.offsite_remote() == ""
    conf.write_text("[gdrive]\ntype = drive\nscope = drive.file\n", encoding="utf-8")
    assert backup.offsite_remote() == "gdrive:Forward Scan Backups"
    with_settings(monkeypatch, backup_offsite_remote="b2:fs-backups", backup_rclone_config=str(conf))
    assert backup.offsite_remote() == "b2:fs-backups"


def test_postgres_backups_are_kept_on_the_data_volume(monkeypatch):
    """The old default (backups/auto) was inside the container: every deploy deleted every backup."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@host.docker.internal:5433/forward_scan?sslmode=disable")
    assert Path(config._default_backup_dir()) == config.ROOT_DIR / "data" / "backups" / "pg-forward_scan"
    # no database name in the url: never the user / password / host as the folder (or Drive folder) name
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:secret@db.example.com")
    assert Path(config._default_backup_dir()).name == "pg-postgres"


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_URL") or not shutil.which("pg_dump") or not shutil.which("pg_restore"),
    reason="needs TEST_POSTGRES_URL and the PostgreSQL client tools (CI)",
)
def test_postgres_full_and_recent_backups_restore_every_scan(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, func, select, text

    import restore_backup
    from app import models
    from app.db import Base
    from migrate_sqlite_to_postgres import normalize_postgres_url

    url = normalize_postgres_url(os.environ["TEST_POSTGRES_URL"])
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    Base.metadata.create_all(eng)
    folder = tmp_path / "pg-backups"
    with_settings(monkeypatch, backup_dir=str(folder), backup_offsite_remote="", backup_rclone_config=str(tmp_path / "none"))
    monkeypatch.setattr(restore_backup, "BACKUPS", folder)

    users, channels = models.User.__table__, models.Channel.__table__
    scans, events = models.Scan.__table__, models.ScanEvent.__table__
    today = date.today()

    def add_scan(c, awb, day=None):
        sid = c.execute(scans.insert().values(tracking_norm=awb, tracking_raw=awb, dispatch_date=day or today, user_id=1,
                                              channel_id=7).returning(scans.c.id)).scalar_one()
        c.execute(events.insert().values(dispatch_date=day or today, user_id=1, channel_id=7, tracking_raw=awb,
                                         tracking_norm=awb, outcome="ACCEPTED", scan_id=sid))

    with eng.begin() as c:
        c.execute(users.insert().values(id=1, username="packer", password_hash="x"))
        c.execute(channels.insert().values(id=7, name="VB - Myntra", marketplace="Myntra"))
        add_scan(c, "PGAWB-BEFORE")
        add_scan(c, "PGAWB-VOIDED", day=today - timedelta(days=5))  # an old scan, outside the recent window

    full = backup._run_full_postgres(url)
    assert full["ok"] and full["verify"]["quick_check"] == "ok" and full["verify"]["checked"]
    dump = folder / "daily" / full["file"]
    assert dump.exists() and (folder / "monthly" / full["file"]).exists()

    with eng.begin() as c:
        add_scan(c, "PGAWB-AFTER")  # only the recent copy has this one
        # the old scan was removed after the full backup and the packet scanned again today
        c.execute(scans.delete().where(scans.c.tracking_norm == "PGAWB-VOIDED"))
        add_scan(c, "PGAWB-VOIDED")
    recent = backup._run_recent_postgres(url)
    assert recent["ok"] and recent["counts"]["scans"] == 3 and recent["counts"]["scan_events"] == 3
    # the recent copy no longer replaces the daily backup or its history
    assert json.loads((folder / "last_full.json").read_text())["file"] == full["file"]
    assert sorted(p.name for p in (folder / "daily").glob("*.dump")) == [full["file"]]

    with eng.begin() as c:  # the disaster
        c.execute(events.delete())
        c.execute(scans.delete())

    out = restore_backup.restore_postgres(dump, folder / "recent" / recent["file"], url=url)
    assert out["scans"] == 3 and Path(out["safety"]).exists()
    with eng.begin() as c:
        rows = c.execute(select(scans.c.tracking_norm, scans.c.dispatch_date)).all()
        assert sorted(t for t, _ in rows) == ["PGAWB-AFTER", "PGAWB-BEFORE", "PGAWB-VOIDED"]
        assert dict(rows)["PGAWB-VOIDED"] == today  # the newer scan from the recent copy, not the removed one
        add_scan(c, "PGAWB-NEXT")  # ids continue after the restored rows
    eng.dispose()
