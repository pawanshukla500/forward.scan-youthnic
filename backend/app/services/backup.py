"""Automatic online backups of the SQLite database, safe while stations are scanning.

The copy is taken through SQLite itself (one read transaction = one consistent snapshot, measured 7 s for a year
of scans with scan latency unaffected), never by copying the .db / -wal / -shm files - a copy of only the .db
silently misses whatever is still in the -wal.

  full   daily: complete snapshot -> PRAGMA quick_check + row counts -> gzip -> daily/ (and monthly/ for the first
         of each month), with a .json manifest (counts, sha256). Optional copy to BACKUP_MIRROR_DIR.
  recent every BACKUP_RECENT_MINUTES: what OMSGuru cannot give back - scans and scan_events of the last
         BACKUP_RECENT_DAYS dispatch days plus users / channels / warehouses / manifests / sync_state (~1 % of a full).

Restore: backend/restore_backup.py (see README "Backups & restore").
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
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


def _prune(folder: Path, pattern: str, keep: int) -> None:
    for old in sorted(folder.glob(pattern))[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        Path(str(old) + ".json").unlink(missing_ok=True)


def _run_full_postgres() -> dict[str, Any]:
    """Complete, verified, custom-format pg_dump into daily/ (+ monthly/ for the month's first)."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from ..db import Base, engine

    dest = backup_dir()
    tmp = dest / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    name = f"fs-pg-{stamp:%Y%m%d-%H%M%S}"
    raw = tmp / f"{name}.dump"
    raw.unlink(missing_ok=True)
    t0 = time.perf_counter()

    u = make_url(settings.database_url)
    cmd = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "-h", u.host or "localhost",
        "-p", str(u.port or 5432),
        "-U", u.username or "postgres",
        "-d", u.database or "forward_scan",
        "-f", str(raw),
    ]

    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = u.password

    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("pg_dump utility not found in PATH")
    except subprocess.CalledProcessError as err:
        msg = err.stderr[:200] if err.stderr else str(err)
        raise RuntimeError(f"pg_dump failed (code {err.returncode}): {msg}")

    if not raw.exists() or raw.stat().st_size == 0:
        raise RuntimeError("pg_dump produced empty or missing file")

    restore_ok = True
    try:
        subprocess.run(["pg_restore", "--list", str(raw)], check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    counts: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            for table in Base.metadata.sorted_tables:
                cnt = conn.execute(text(f'SELECT count(*) FROM "{table.name}"')).fetchone()[0]
                counts[table.name] = cnt
    except Exception as exc:
        log.warning("Could not query table counts for pg_dump manifest: %s", exc)

    rep: dict[str, Any] = {
        "kind": "full", "backend": "postgresql", "source": u.database,
        "started": stamp.isoformat(timespec="seconds"), "copy_s": round(time.perf_counter() - t0, 2),
        "verify": {"quick_check": "ok" if restore_ok else "warn", "ok": restore_ok, "counts": counts},
        "file": raw.name, "bytes": raw.stat().st_size, "sha256": _sha256(raw),
    }

    daily = dest / "daily"
    daily.mkdir(exist_ok=True)
    landed = daily / raw.name
    os.replace(raw, landed)
    _write_json(Path(str(landed) + ".json"), rep)

    monthly = dest / "monthly"
    monthly.mkdir(exist_ok=True)
    if not any(monthly.glob(f"fs-pg-{stamp:%Y%m}*.dump")):
        shutil.copyfile(landed, monthly / landed.name)
        _write_json(monthly / (landed.name + ".json"), rep)

    _prune(daily, "fs-pg-*.dump", settings.backup_keep_daily)
    _prune(monthly, "fs-pg-*.dump", settings.backup_keep_monthly)

    rep["mirror"] = _mirror([landed, Path(str(landed) + ".json")], "daily")
    rep["ok"] = True
    rep["total_s"] = round(time.perf_counter() - t0, 2)
    _write_json(dest / "last_full.json", rep)
    log.info("Full PostgreSQL backup %s (%.1f MB) in %.1fs", landed.name, rep["bytes"] / 1e6, rep["total_s"])
    return rep


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
    daily = dest / "daily"
    daily.mkdir(exist_ok=True)
    landed = daily / final.name
    os.replace(final, landed)
    _write_json(Path(str(landed) + ".json"), rep)
    monthly = dest / "monthly"
    monthly.mkdir(exist_ok=True)
    if not any(monthly.glob(f"fs-{stamp:%Y%m}*.db.gz")):
        shutil.copyfile(landed, monthly / landed.name)
        _write_json(monthly / (landed.name + ".json"), rep)
    _prune(daily, "fs-*.db.gz", settings.backup_keep_daily)
    _prune(monthly, "fs-*.db.gz", settings.backup_keep_monthly)
    rep["mirror"] = _mirror([landed, Path(str(landed) + ".json")], "daily")
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


def _run_recent_postgres() -> dict[str, Any]:
    dest = backup_dir()
    rep = _run_full_postgres()
    recent_rep = dict(rep)
    recent_rep["kind"] = "recent"
    _write_json(dest / "last_recent.json", recent_rep)
    return recent_rep


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
    _write_json(Path(str(out) + ".json"), rep)
    _prune(dest, "fs-recent-*.db", settings.backup_keep_recent)
    rep["mirror"] = _mirror([out, Path(str(out) + ".json")], "recent")
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
        err = _read_json(dest / f"last_{kind}_error.json")
        if err and (not last or err["at"] > last.get("started", "")):
            problems.append(f"The last {'full backup' if kind == 'full' else 'copy of recent scans'} failed at "
                            f"{err['at'][11:16]} - see the server log")
        m = (last or {}).get("mirror")
        if m and not m.get("ok"):
            problems.append(f"Copy to {m.get('dir')} failed: {m.get('error', 'checksum mismatch')}")

    mirror = settings.backup_mirror_dir
    free = free_gb(dest)
    if free is not None and free < 15.0:
        problems.append(f"Only {free} GB free on the database disk")

    if is_postgres():
        if not mirror:
            problems.append("Backups are only on this server's disk - set BACKUP_MIRROR_DIR to an offsite copy")
        return {
            "enabled": settings.backup_enabled, "backend": "postgresql", "dir": str(dest),
            "mirror_dir": settings.backup_mirror_dir or None,
            "full": full and {k: full.get(k) for k in ("started", "ok", "bytes", "file", "total_s", "mirror")},
            "full_age_hours": full_age,
            "recent": recent and {k: recent.get(k) for k in ("started", "ok", "bytes", "file", "seconds", "mirror")},
            "recent_age_hours": recent_age, "recent_minutes": settings.backup_recent_minutes,
            "db_bytes": None, "wal_bytes": 0,
            "disk_free_gb": free, "running": sorted(_running), "problems": problems,
        }

    # SQLite
    db = db_path()
    wal = Path(str(db) + "-wal")
    same_disk = not mirror or Path(mirror).drive.lower() == db.drive.lower()
    if same_disk:
        problems.append("Backups are only on this PC's disk - set BACKUP_MIRROR_DIR to a NAS, USB disk or another PC")

    return {
        "enabled": settings.backup_enabled, "backend": "sqlite", "dir": str(dest),
        "mirror_dir": settings.backup_mirror_dir or None,
        "full": full and {k: full.get(k) for k in ("started", "ok", "bytes", "file", "total_s", "mirror")},
        "full_age_hours": full_age,
        "recent": recent and {k: recent.get(k) for k in ("started", "ok", "bytes", "file", "seconds", "mirror")},
        "recent_age_hours": recent_age, "recent_minutes": settings.backup_recent_minutes,
        "db_bytes": db.stat().st_size if db.exists() else None, "wal_bytes": wal.stat().st_size if wal.exists() else 0,
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
