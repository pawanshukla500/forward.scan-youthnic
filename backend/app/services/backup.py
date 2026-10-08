"""Automatic online backups of the database (SQLite or PostgreSQL), safe while stations are scanning.

  full   daily: complete snapshot -> checked -> daily/ (and monthly/ for the first of each month), with a .json
         manifest (counts, sha256).
           SQLite     copied through SQLite itself (one read transaction = one consistent snapshot), never by
                      copying the .db / -wal / -shm files - a copy of only the .db silently misses the -wal.
                      PRAGMA quick_check + row counts, gzip.
           PostgreSQL pg_dump --format=custom (compressed); pg_restore --list must read it back and find the
                      main tables, or the dump is kept aside as .FAILED and the Admin page turns red.
  recent every BACKUP_RECENT_MINUTES: what OMSGuru cannot give back - scans and scan_events of the last
         BACKUP_RECENT_DAYS dispatch days plus users / channels / warehouses / manifests / sync_state (~1 % of a
         full), read in ONE snapshot into a small SQLite file in recent/. (PostgreSQL used to run a whole pg_dump
         here into daily/, which pruned the daily history down to the last few hours.)

Copies elsewhere - without one, a lost server disk loses every backup:
  BACKUP_MIRROR_DIR      another disk / NAS / USB path the server can write to.
  offsite (rclone)       BACKUP_OFFSITE_REMOTE, or automatically the "gdrive" remote in data/rclone/rclone.conf:
                         every full backup, and the newest recent copy at most every BACKUP_OFFSITE_RECENT_MINUTES,
                         is uploaded to <remote>/<backup folder>/{daily,monthly,recent} and checked (size + md5).
                         Copies older than BACKUP_OFFSITE_KEEP_DAYS are removed from the cloud.

Restore: backend/restore_backup.py (see README "Backups & restore").
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import settings

log = logging.getLogger("backup")

RECENT_FULL_TABLES = ("users", "channels", "warehouses", "manifests", "sync_state")
RECENT_WINDOW_TABLES = ("scans", "scan_events")  # windowed by dispatch_date
# A PostgreSQL dump without these is not a usable backup of Forward Scan.
REQUIRED_PG_TABLES = ("users", "channels", "scans", "scan_events")

# Admin shows these amber ("one copy only"), every other problem red.
ONLY_LOCAL_PC = "Backups are only on this PC's disk - set BACKUP_MIRROR_DIR to a NAS, USB disk or another PC"
ONLY_LOCAL_SERVER = ("Backups are only on this server's disk - connect the offsite copy "
                     "(README: Backups & restore -> Offsite copy)")


def is_postgres() -> bool:
    return not settings.database_url.startswith("sqlite")


def enabled() -> bool:
    return bool(settings.backup_enabled)


def db_path() -> Path:
    if settings.database_url.startswith("sqlite:///"):
        return Path(settings.database_url[len("sqlite:///"):])
    return Path(settings.backup_dir)


def backup_dir() -> Path:
    return Path(settings.backup_dir)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5(p: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - only to compare with the cloud's own md5, not for security
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(p: Path, data: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _check(path: Path) -> dict[str, Any]:
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        qc = c.execute("PRAGMA quick_check").fetchone()[0]
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts = {n: c.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        c.close()
    return {"quick_check": qc, "ok": qc == "ok", "counts": counts}


def _source() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path().as_posix()}?mode=ro", uri=True, timeout=30)


def _mirror(files: list[Path], sub: str) -> dict[str, Any] | None:
    """Copy to a second place (another physical disk / NAS / USB) and verify the bytes that arrived."""
    if not settings.backup_mirror_dir:
        return None
    try:
        target = Path(settings.backup_mirror_dir) / sub
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            part = target / (f.name + ".partial")
            shutil.copyfile(f, part)
            os.replace(part, target / f.name)
        ok = _sha256(target / files[0].name) == _sha256(files[0])
        return {"dir": str(target), "ok": ok}
    except OSError as exc:
        return {"dir": settings.backup_mirror_dir, "ok": False, "error": str(exc)[:200]}


# ---- offsite copy (rclone: Google Drive, S3, ...) ---------------------------------------------------


def offsite_remote() -> str:
    """Where the offsite copies go, e.g. "gdrive:Forward Scan Backups"; "" = none set up."""
    if settings.backup_offsite_remote:
        return settings.backup_offsite_remote
    try:
        if "[gdrive]" in Path(settings.backup_rclone_config).read_text(encoding="utf-8"):
            return "gdrive:Forward Scan Backups"
    except OSError:
        pass
    return ""


def _rclone(args: list[str], timeout: int = 900) -> str:
    env = dict(os.environ, RCLONE_CONFIG=settings.backup_rclone_config)
    try:
        r = subprocess.run(["rclone", *args], env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("rclone is not installed on this server") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"rclone {args[0]} took longer than {timeout} s") from exc
    if r.returncode != 0:
        raise RuntimeError(f"rclone {args[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def _offsite(files: list[Path], sub: str, keep_days: int) -> dict[str, Any] | None:
    """Upload [backup, manifest] to <remote>/<backup folder>/<sub>, then read the backup's size and md5 back."""
    remote = offsite_remote()
    if not remote:
        return None
    target = f"{remote.rstrip('/')}/{backup_dir().name}/{sub}"
    at = datetime.now().isoformat(timespec="seconds")
    try:
        for f in files:
            _rclone(["copyto", "--retries", "3", str(f), f"{target}/{f.name}"])
        listing = json.loads(_rclone(["lsjson", "--hash", "--files-only", f"{target}/{files[0].name}"], timeout=300) or "[]")
        entry = listing[0] if listing else {}
        remote_md5 = (entry.get("Hashes") or {}).get("md5")
        ok = bool(entry) and entry.get("Size") == files[0].stat().st_size and remote_md5 in (None, _md5(files[0]))
        res: dict[str, Any] = {"remote": target, "ok": ok, "at": at, "file": files[0].name,
                               "checked": "size + md5" if remote_md5 else "size"}
        if not ok:
            res["error"] = "the copy in the cloud does not match (size / checksum)"
    except (RuntimeError, ValueError, OSError) as exc:
        return {"remote": target, "ok": False, "at": at, "error": str(exc)[:300]}
    try:  # older copies in the cloud; a failure here never fails the backup
        _rclone(["delete", "--drive-use-trash=false", "--min-age", f"{keep_days}d", target], timeout=300)
    except RuntimeError as exc:
        log.warning("Could not remove old offsite copies in %s: %s", target, exc)
    return res


def _prune(folder: Path, pattern: str, keep: int) -> None:
    for old in sorted(folder.glob(pattern))[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        Path(str(old) + ".json").unlink(missing_ok=True)


def _land_full(tmp_file: Path, pattern: str, monthly_glob: str, rep: dict[str, Any]) -> Path:
    """Move a checked full backup into daily/ (+ monthly/), prune, copy to the mirror and offsite."""
    dest = backup_dir()
    daily = dest / "daily"
    daily.mkdir(exist_ok=True)
    landed = daily / tmp_file.name
    os.replace(tmp_file, landed)
    manifest = Path(str(landed) + ".json")
    _write_json(manifest, rep)
    monthly = dest / "monthly"
    monthly.mkdir(exist_ok=True)
    new_month = not any(monthly.glob(monthly_glob))
    if new_month:
        shutil.copyfile(landed, monthly / landed.name)
        _write_json(monthly / manifest.name, rep)
    _prune(daily, pattern, settings.backup_keep_daily)
    _prune(monthly, pattern, settings.backup_keep_monthly)
    rep["mirror"] = _mirror([landed, manifest], "daily")
    rep["offsite"] = _offsite([landed, manifest], "daily", settings.backup_offsite_keep_days)
    if new_month and rep["offsite"] and rep["offsite"].get("ok"):
        rep["offsite_monthly"] = _offsite([monthly / landed.name, monthly / manifest.name], "monthly",
                                          max(settings.backup_offsite_keep_days, settings.backup_keep_monthly * 31 + 5))
    return landed


# ---- PostgreSQL ---------------------------------------------------------------------------------------


def pg_conn_args(url: str | None = None) -> tuple[list[str], dict[str, str], str]:
    """-h/-p/-U arguments, environment (PGPASSWORD, PGSSLMODE) and database name for pg_dump / pg_restore."""
    from sqlalchemy.engine import make_url

    u = make_url(url or settings.database_url)
    args = ["-h", u.host or "localhost", "-p", str(u.port or 5432), "-U", u.username or "postgres"]
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = u.password
    sslmode = u.query.get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = sslmode if isinstance(sslmode, str) else sslmode[0]
    return args, env, u.database or "forward_scan"


def pg_run(cmd: list[str], env: dict[str, str], timeout: int = 3600) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{cmd[0]} is not installed (postgresql-client)") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{cmd[0]} took longer than {timeout} s") from exc
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed (code {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def verify_pg_dump(path: Path) -> dict[str, Any]:
    """pg_restore reads the dump's whole table of contents: a truncated or damaged file fails here."""
    try:
        r = subprocess.run(["pg_restore", "--list", str(path)], capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return {"quick_check": "not checked (pg_restore missing)", "ok": True, "checked": False}
    if r.returncode != 0:
        return {"quick_check": f"pg_restore cannot read it: {(r.stderr or '').strip()[-200:]}", "ok": False}
    missing = [t for t in REQUIRED_PG_TABLES if not re.search(rf"TABLE DATA public {t}(\s|$)", r.stdout, re.M)]
    if missing:
        return {"quick_check": f"tables missing from the dump: {', '.join(missing)}", "ok": False}
    return {"quick_check": "ok", "ok": True, "checked": True}


def _pg_engine(url: str):
    from sqlalchemy import create_engine

    from ..db import engine

    return engine if url == settings.database_url else create_engine(url)


def _pg_counts(url: str) -> dict[str, int]:
    from sqlalchemy import text

    from .. import models  # noqa: F401 - registers the tables (a one-off script has not imported them yet)
    from ..db import Base

    counts: dict[str, int] = {}
    try:
        with _pg_engine(url).connect() as conn:
            for table in Base.metadata.sorted_tables:
                counts[table.name] = conn.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar_one()
    except Exception as exc:  # noqa: BLE001 - counts are informative only
        log.warning("Could not count rows for the backup manifest: %s", exc)
    return counts


def _run_full_postgres(url: str | None = None) -> dict[str, Any]:
    """Complete pg_dump (custom format, compressed), read back with pg_restore, into daily/ (+ monthly/)."""
    url = url or settings.database_url
    dest = backup_dir()
    tmp = dest / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    raw = tmp / f"fs-pg-{stamp:%Y%m%d-%H%M%S}.dump"
    raw.unlink(missing_ok=True)
    t0 = time.perf_counter()

    args, env, dbname = pg_conn_args(url)
    pg_run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", *args, "-d", dbname, "-f", str(raw)], env)
    if not raw.exists() or raw.stat().st_size == 0:
        raise RuntimeError("pg_dump produced an empty or missing file")

    verify = verify_pg_dump(raw)
    verify["counts"] = _pg_counts(url)
    rep: dict[str, Any] = {
        "kind": "full", "backend": "postgresql", "source": dbname,
        "started": stamp.isoformat(timespec="seconds"), "copy_s": round(time.perf_counter() - t0, 2),
        "verify": verify, "file": raw.name, "bytes": raw.stat().st_size, "sha256": _sha256(raw),
    }
    if not verify["ok"]:
        raw.rename(raw.with_suffix(".dump.FAILED"))
        rep["ok"] = False
        _write_json(dest / "last_full.json", rep)
        raise RuntimeError(f"backup check failed: {verify['quick_check']}")

    landed = _land_full(raw, "fs-pg-*.dump", f"fs-pg-{stamp:%Y%m}*.dump", rep)
    rep["ok"] = True
    rep["total_s"] = round(time.perf_counter() - t0, 2)
    _write_json(dest / "last_full.json", rep)
    log.info("Full PostgreSQL backup %s (%.1f MB) in %.1fs", landed.name, rep["bytes"] / 1e6, rep["total_s"])
    return rep


def _run_recent_postgres(url: str | None = None) -> dict[str, Any]:
    """Recent scans / events + the small tables, read in one REPEATABLE READ snapshot into a small SQLite file
    (same format as the SQLite recent copy; restore_backup.py merges it into PostgreSQL)."""
    from sqlalchemy import create_engine, select

    from .. import models  # noqa: F401 - registers every table on Base.metadata
    from ..db import Base

    url = url or settings.database_url
    dest = backup_dir() / "recent"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    out = dest / f"fs-recent-{stamp:%Y%m%d-%H%M%S}.db"
    part = out.with_suffix(".partial")
    part.unlink(missing_ok=True)
    since_day = date.today() - timedelta(days=settings.backup_recent_days)
    since = since_day.isoformat()
    t0 = time.perf_counter()
    tables = [Base.metadata.tables[n] for n in RECENT_FULL_TABLES + RECENT_WINDOW_TABLES]
    counts: dict[str, int] = {}
    dst_engine = create_engine(f"sqlite:///{part.as_posix()}")
    try:
        Base.metadata.create_all(dst_engine, tables=tables)
        with _pg_engine(url).connect() as src_conn, dst_engine.begin() as dst:
            src = src_conn.execution_options(isolation_level="REPEATABLE READ")
            with src.begin():  # one snapshot across all tables
                for t in tables:
                    q = select(t)
                    if t.name in RECENT_WINDOW_TABLES:
                        q = q.where(t.c.dispatch_date >= since_day)
                    res = src.execute(q.order_by(*t.primary_key.columns))
                    n = 0
                    while rows := res.fetchmany(5000):
                        dst.execute(t.insert(), [dict(r._mapping) for r in rows])
                        n += len(rows)
                    counts[t.name] = n
    finally:
        dst_engine.dispose()
    c = sqlite3.connect(part)
    try:
        c.execute("CREATE TABLE _recent_meta (k TEXT PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO _recent_meta VALUES (?, ?)",
                      [("since_dispatch_date", since), ("taken_at", stamp.isoformat(timespec="seconds")),
                       ("backend", "postgresql")])
        c.commit()
    finally:
        c.close()
    os.replace(part, out)
    _, _, dbname = pg_conn_args(url)
    rep = {"kind": "recent", "backend": "postgresql", "source": dbname, "file": out.name, "since": since,
           "counts": counts, "bytes": out.stat().st_size, "started": stamp.isoformat(timespec="seconds"),
           "seconds": round(time.perf_counter() - t0, 2), "quick_check": _check(out)["quick_check"],
           "sha256": _sha256(out)}
    rep["ok"] = rep["quick_check"] == "ok"
    return _finish_recent(out, rep)


# ---- SQLite -------------------------------------------------------------------------------------------


def _run_full_sqlite() -> dict[str, Any]:
    dest = backup_dir()
    tmp = dest / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    name = f"fs-{stamp:%Y%m%d-%H%M%S}"
    raw = tmp / f"{name}.db"
    raw.unlink(missing_ok=True)
    t0 = time.perf_counter()
    src = _source()
    try:
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst, pages=-1)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    rep: dict[str, Any] = {"kind": "full", "source": str(db_path()), "started": stamp.isoformat(timespec="seconds"),
                           "copy_s": round(time.perf_counter() - t0, 2)}
    rep["verify"] = _check(raw)
    if not rep["verify"]["ok"]:
        raw.rename(raw.with_suffix(".db.FAILED"))
        rep["ok"] = False
        _write_json(dest / "last_full.json", rep)
        raise RuntimeError(f"backup check failed: {rep['verify']['quick_check']}")
    final = tmp / f"{name}.db.gz"
    with open(raw, "rb") as fi, gzip.open(final, "wb", compresslevel=1) as fo:
        shutil.copyfileobj(fi, fo, 4 << 20)
    raw.unlink()
    rep.update(file=final.name, bytes=final.stat().st_size, sha256=_sha256(final))
    landed = _land_full(final, "fs-*.db.gz", f"fs-{stamp:%Y%m}*.db.gz", rep)
    rep["ok"] = True
    rep["total_s"] = round(time.perf_counter() - t0, 2)
    _write_json(dest / "last_full.json", rep)
    log.info("Full backup %s (%.1f MB) in %.1fs", landed.name, rep["bytes"] / 1e6, rep["total_s"])
    return rep


def run_full() -> dict[str, Any]:
    """Complete, verified, compressed snapshot into daily/ (+ monthly/ for the month's first)."""
    if is_postgres():
        return _run_full_postgres()
    return _run_full_sqlite()


def _run_recent_sqlite() -> dict[str, Any]:
    dest = backup_dir() / "recent"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    out = dest / f"fs-recent-{stamp:%Y%m%d-%H%M%S}.db"
    part = out.with_suffix(".partial")
    part.unlink(missing_ok=True)
    since = (date.today() - timedelta(days=settings.backup_recent_days)).isoformat()
    t0 = time.perf_counter()
    src = _source()
    src.isolation_level = None
    dst = sqlite3.connect(part)
    counts: dict[str, int] = {}
    try:
        schema = dict(src.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))
        src.execute("BEGIN")  # one read snapshot across all tables
        for table in RECENT_FULL_TABLES + RECENT_WINDOW_TABLES:
            dst.execute(schema[table])
            where, args = ("WHERE dispatch_date >= ?", (since,)) if table in RECENT_WINDOW_TABLES else ("", ())
            cur = src.execute(f'SELECT * FROM "{table}" {where}', args)
            marks = ",".join("?" * len(cur.description))
            n = 0
            while rows := cur.fetchmany(5000):
                dst.executemany(f'INSERT INTO "{table}" VALUES ({marks})', rows)
                n += len(rows)
            counts[table] = n
        dst.execute("CREATE TABLE _recent_meta (k TEXT PRIMARY KEY, v TEXT)")
        dst.executemany("INSERT INTO _recent_meta VALUES (?, ?)",
                        [("since_dispatch_date", since), ("taken_at", stamp.isoformat(timespec="seconds"))])
        dst.commit()
        src.execute("COMMIT")
    finally:
        src.close()
        dst.close()
    os.replace(part, out)
    rep = {"kind": "recent", "source": str(db_path()), "file": out.name, "since": since, "counts": counts,
           "bytes": out.stat().st_size,
           "started": stamp.isoformat(timespec="seconds"), "seconds": round(time.perf_counter() - t0, 2),
           "quick_check": _check(out)["quick_check"], "sha256": _sha256(out)}
    rep["ok"] = rep["quick_check"] == "ok"
    return _finish_recent(out, rep)


def _finish_recent(out: Path, rep: dict[str, Any]) -> dict[str, Any]:
    """Manifest, prune, mirror, and an offsite upload at most every BACKUP_OFFSITE_RECENT_MINUTES."""
    dest = out.parent
    manifest = Path(str(out) + ".json")
    _write_json(manifest, rep)
    _prune(dest, "fs-recent-*.db", settings.backup_keep_recent)
    rep["mirror"] = _mirror([out, manifest], "recent")
    prev = (_read_json(backup_dir() / "last_recent.json") or {}).get("offsite")
    prev_age = _age_hours(prev.get("at")) if prev else None
    if prev and prev.get("ok") and prev_age is not None and prev_age * 60 < settings.backup_offsite_recent_minutes:
        rep["offsite"] = prev  # uploaded a little while ago: keep the cloud quiet
    else:
        rep["offsite"] = _offsite([out, manifest], "recent", 3)
    _write_json(backup_dir() / "last_recent.json", rep)
    return rep


def run_recent() -> dict[str, Any]:
    """Recent scans / events + the small tables, read in ONE snapshot of the live database."""
    if is_postgres():
        return _run_recent_postgres()
    return _run_recent_sqlite()


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    return round((datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 3600, 2)


def _pg_size() -> int | None:
    from sqlalchemy import text

    try:
        with _pg_engine(settings.database_url).connect() as conn:
            return int(conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
    except Exception:  # noqa: BLE001 - informative only
        return None


def status() -> dict[str, Any]:
    """Health of the backups for the Admin page."""
    dest = backup_dir()
    full = _read_json(dest / "last_full.json")
    recent = _read_json(dest / "last_recent.json")

    def free_gb(p: Path) -> float | None:
        try:
            return round(shutil.disk_usage(p if p.exists() else p.parent).free / 1e9, 1)
        except OSError:
            return None

    full_age, recent_age = _age_hours(full and full.get("started")), _age_hours(recent and recent.get("started"))
    problems = []
    if not settings.backup_enabled:
        problems.append("Automatic backups are switched off (BACKUP_ENABLED=false)")
    elif full is None:
        problems.append("No full backup yet")
    elif not full.get("ok"):
        problems.append("The last full backup failed its check")
    elif full_age is not None and full_age > 26:
        problems.append(f"Last full backup is {full_age:.0f} hours old")
    if settings.backup_enabled and recent and recent_age is not None and recent_age > 1:
        problems.append(f"Last copy of recent scans is {recent_age:.1f} hours old")

    for kind, last in (("full", full), ("recent", recent)):
        what = "full backup" if kind == "full" else "copy of recent scans"
        err = _read_json(dest / f"last_{kind}_error.json")
        if err and (not last or err["at"] > last.get("started", "")):
            problems.append(f"The last {what} failed at {err['at'][11:16]} - see the server log")
        m = (last or {}).get("mirror")
        if m and not m.get("ok"):
            problems.append(f"Copy to {m.get('dir')} failed: {m.get('error', 'checksum mismatch')}")
        o = (last or {}).get("offsite")
        if o and not o.get("ok"):
            problems.append(f"Offsite copy of the {what} failed: {o.get('error', 'not checked')}")

    mirror = settings.backup_mirror_dir
    remote = offsite_remote()
    full_off = (full or {}).get("offsite")
    if remote and settings.backup_enabled and full and full.get("ok") and not full_off:
        problems.append("No offsite copy yet - press \"Back up now\" (or wait for tonight's backup) to upload one")
    free = free_gb(dest)
    if free is not None and free < 15.0:
        problems.append(f"Only {free} GB free on the database disk")

    pg = is_postgres()
    if pg:
        db_bytes, wal_bytes = _pg_size(), 0
        if not mirror and not remote:
            problems.append(ONLY_LOCAL_SERVER)
    else:
        db = db_path()
        wal = Path(str(db) + "-wal")
        db_bytes = db.stat().st_size if db.exists() else None
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        same_disk = not mirror or Path(mirror).drive.lower() == db.drive.lower()
        if same_disk and not remote:
            problems.append(ONLY_LOCAL_PC)

    keys_full = ("started", "ok", "bytes", "file", "total_s", "mirror", "offsite")
    keys_recent = ("started", "ok", "bytes", "file", "seconds", "mirror", "offsite")
    return {
        "enabled": settings.backup_enabled, "backend": "postgresql" if pg else "sqlite", "dir": str(dest),
        "mirror_dir": mirror or None,
        "offsite": {"remote": remote or None, "full": full_off, "recent": (recent or {}).get("offsite"),
                    "keep_days": settings.backup_offsite_keep_days},
        "full": full and {k: full.get(k) for k in keys_full},
        "full_age_hours": full_age,
        "recent": recent and {k: recent.get(k) for k in keys_recent},
        "recent_age_hours": recent_age, "recent_minutes": settings.backup_recent_minutes,
        "db_bytes": db_bytes, "wal_bytes": wal_bytes,
        "disk_free_gb": free, "running": sorted(_running), "problems": problems,
    }


def full_due(now: datetime | None = None) -> bool:
    """Once a day, after BACKUP_FULL_HOUR (local time) - or straight away when there is none yet."""
    now = now or datetime.now()
    last = _read_json(backup_dir() / "last_full.json")
    if not last or not last.get("ok"):
        return True
    taken = datetime.fromisoformat(last["started"])
    if taken.date() == now.date():
        return False
    return now.hour >= settings.backup_full_hour or (now - taken) > timedelta(hours=30)


def recent_due() -> bool:
    last = _read_json(backup_dir() / "last_recent.json")
    if not last:
        return True
    return (datetime.now() - datetime.fromisoformat(last["started"])) >= timedelta(minutes=settings.backup_recent_minutes)


# ---- scheduler (runs inside the server process) -------------------------------------------------

_requested: set[str] = set()
_running: dict[str, float] = {}


def request(kind: str) -> None:
    """Admin "Back up now": picked up by the scheduler within a few seconds."""
    _requested.add(kind)


def _fingerprint() -> tuple[int, int]:
    if is_postgres():
        from sqlalchemy import text
        from ..db import engine
        try:
            with engine.connect() as conn:
                max_s = conn.execute(text("SELECT coalesce(max(id), 0) FROM scans")).fetchone()[0]
                max_e = conn.execute(text("SELECT coalesce(max(id), 0) FROM scan_events")).fetchone()[0]
                return (int(max_s), int(max_e))
        except Exception:
            return (0, 0)
    src = _source()
    try:
        return (src.execute("SELECT coalesce(max(id), 0) FROM scans").fetchone()[0],
                src.execute("SELECT coalesce(max(id), 0) FROM scan_events").fetchone()[0])
    finally:
        src.close()


async def run_forever() -> None:
    """Daily full backup + a copy of recent scans every BACKUP_RECENT_MINUTES (skipped when nothing changed)."""
    import asyncio

    await asyncio.sleep(20)  # let startup and the first sync finish
    last_print: tuple[int, int] | None = None
    while True:
        for kind, due, job in (("full", full_due, run_full), ("recent", recent_due, run_recent)):
            try:
                forced = kind in _requested
                if not forced and not await asyncio.to_thread(due):
                    continue
                if kind == "recent" and not forced:
                    fp = await asyncio.to_thread(_fingerprint)
                    if fp == last_print:
                        continue
                    last_print = fp
                _requested.discard(kind)
                _running[kind] = time.time()
                await asyncio.to_thread(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed backup is reported on the Admin page, never fatal
                log.exception("%s backup failed", kind)
                _write_json(backup_dir() / f"last_{kind}_error.json",
                            {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind})
            finally:
                _running.pop(kind, None)
        await asyncio.sleep(5 if _requested else 30)
