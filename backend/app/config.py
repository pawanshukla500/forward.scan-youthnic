"""Application settings, loaded from the project-root .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def normalize_database_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip() or "sqlite:///omsguru_forward_scan.db"
    # Relative sqlite paths resolve against the project root, not the cwd.
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url[len("sqlite:///"):]
        if not (len(rel) > 1 and rel[1] == ":"):  # not a Windows absolute path
            target_path = ROOT_DIR / rel
            target_path.parent.mkdir(parents=True, exist_ok=True)
            url = "sqlite:///" + target_path.as_posix()
        else:
            Path(rel).parent.mkdir(parents=True, exist_ok=True)
    elif url.startswith("sqlite:////"):
        abs_path = Path("/" + url[len("sqlite:////"):])
        abs_path.parent.mkdir(parents=True, exist_ok=True)
    return normalize_database_url(url)


def _default_backup_dir() -> str:
    """backups/<database name> next to the database: every database gets its own folder, so a test or demo
    database can never land in (and later be restored over) the live database's backups."""
    url = _database_url()
    if url.startswith("sqlite:///"):
        db = Path(url[len("sqlite:///"):])
        return str(db.parent / "backups" / db.stem)
    # PostgreSQL: under data/ - the folder the server keeps across deploys (docker-compose mounts ./data).
    # The old default "backups/auto" sat inside the container, so every deploy threw the backups away.
    try:
        from sqlalchemy.engine import make_url

        name = make_url(url).database or "postgres"
    except Exception:  # noqa: BLE001 - an unparsable url is reported by the engine, not here
        name = "postgres"
    return str(ROOT_DIR / "data" / "backups" / f"pg-{name}")


def _path_setting(name: str, default: str) -> str:
    p = Path(os.getenv(name, "").strip() or default)
    return str(p if p.is_absolute() else ROOT_DIR / p)


@dataclass(frozen=True)
class Settings:
    oms_base_url: str = os.getenv("OMSGURU_BASE_URL", "https://client.omsguru.com").rstrip("/")
    oms_token: str = os.getenv("OMSGURU_API_TOKEN", "")
    oms_client_id: str = os.getenv("OMSGURU_CLIENT_ID", "")
    oms_use_mock: bool = _bool("OMSGURU_USE_MOCK", False)

    # Seconds between incremental invoice syncs (each run costs >= 1 API credit).
    sync_interval_seconds: int = max(15, _int("SYNC_INTERVAL_SECONDS", 60))
    # Page size for list endpoints (OMSGuru max is 100).
    batch_limit: int = min(100, max(1, _int("BATCH_LIMIT", 100)))
    # How far back to look for open (Packed / Ready-to-ship) orders.
    order_lookback_days: int = min(45, max(1, _int("ORDER_LOOKBACK_DAYS", 15)))
    # Minutes between full open-order refreshes and cancellation sweeps.
    open_orders_refresh_minutes: int = max(5, _int("OPEN_ORDERS_REFRESH_MINUTES", 30))
    cancel_sweep_minutes: int = max(5, _int("CANCEL_SWEEP_MINUTES", 20))
    # Cancellation check looks this many days back (by order date) - only to flag orders already in the working set.
    cancel_check_days: int = min(45, max(1, _int("CANCEL_CHECK_DAYS", 3)))
    # Days of unscanned order data kept, by AWB generation date (channel-wise reconciliation, duplicate / cancel checks).
    # Orders still Packed / Ready-to-ship are always kept - they are pending.
    retain_orders_days: int = min(45, max(1, _int("RETAIN_ORDERS_DAYS", 7)))
    # An unscanned (not cancelled) AWB stays pending until it is scanned - but at most this many days, so a label
    # that can never be scanned (replaced AWB, lost packet) does not sit in Overdue forever.
    pending_keep_days: int = min(365, max(7, _int("PENDING_KEEP_DAYS", 45)))
    # Scanned data - scans with their order details and audit events, and the orders they belong to - is kept this
    # many whole calendar years from the scan date (owner, 10 Oct 2026: at least 2 years); what is older is removed
    # once a month, on RETENTION_DAY (the 10th), each removed scan first written to backups/removed-scans/.
    # 0 = keep forever. Whole years only, so a typo can never mean days.
    scan_retention_years: int = max(0, _int("SCAN_RETENTION_YEARS", 2))
    retention_day: int = min(28, max(1, _int("RETENTION_DAY", 10)))
    # Fill the last RETAIN_ORDERS_DAYS of AWBs in the background, using only spare API credits.
    history_backfill: bool = _bool("HISTORY_BACKFILL", True)
    # Credits we always leave untouched for other integrations sharing this client id.
    oms_reserve_credits: int = max(0, _int("OMSGURU_RESERVE_CREDITS", 5))
    sync_enabled: bool = _bool("SYNC_ENABLED", True)
    # Every scan asks OMSGuru live (order_details by order id) for fresh status + details.
    live_lookup: bool = _bool("LIVE_LOOKUP", True)
    # Max seconds a scan may spend on live OMSGuru calls before falling back to the local copy.
    live_timeout_seconds: float = float(os.getenv("LIVE_TIMEOUT_SECONDS", "2.5") or 2.5)
    # Credits background sync leaves untouched so scan-time calls almost always find one free.
    live_headroom: int = max(0, _int("LIVE_HEADROOM", 4))
    # Numbers every page re-reads after each scan are recomputed at most this often (seconds) per channel.
    cache_min_interval: float = max(0.0, float(os.getenv("CACHE_MIN_INTERVAL", "1.0") or 1.0))

    database_url: str = field(default_factory=_database_url)

    # --- automatic backups (SQLite and PostgreSQL; see services/backup.py) ---
    backup_enabled: bool = _bool("BACKUP_ENABLED", True)
    backup_dir: str = field(default_factory=lambda: _path_setting("BACKUP_DIR", _default_backup_dir()))
    # A second place on ANOTHER physical disk (NAS share, USB disk, another PC). Empty = backups stay on this PC.
    backup_mirror_dir: str = os.getenv("BACKUP_MIRROR_DIR", "").strip()
    backup_full_hour: int = min(23, max(0, _int("BACKUP_FULL_HOUR", 2)))  # daily full backup after this local hour
    backup_recent_minutes: int = max(5, _int("BACKUP_RECENT_MINUTES", 15))
    backup_recent_days: int = max(1, _int("BACKUP_RECENT_DAYS", 2))
    backup_keep_daily: int = max(1, _int("BACKUP_KEEP_DAILY", 14))
    backup_keep_monthly: int = max(0, _int("BACKUP_KEEP_MONTHLY", 12))
    backup_keep_recent: int = max(4, _int("BACKUP_KEEP_RECENT", 96))
    # Offsite copy with rclone (e.g. "gdrive:Forward Scan Backups"). Empty = automatic: the "gdrive" remote when
    # data/rclone/rclone.conf has one (set up once on the server, kept across deploys), else no offsite copy.
    backup_offsite_remote: str = os.getenv("BACKUP_OFFSITE_REMOTE", "").strip()
    backup_rclone_config: str = field(
        default_factory=lambda: _path_setting("RCLONE_CONFIG", str(ROOT_DIR / "data" / "rclone" / "rclone.conf"))
    )
    backup_offsite_keep_days: int = max(7, _int("BACKUP_OFFSITE_KEEP_DAYS", 60))  # daily copies kept in the cloud
    backup_offsite_recent_minutes: int = max(15, _int("BACKUP_OFFSITE_RECENT_MINUTES", 60))  # recent scans to the cloud

    secret_key: str = os.getenv("APP_SECRET_KEY", "")
    token_hours: int = _int("SESSION_HOURS", 14)
    # The default admin: created at startup when no account has ADMIN_EMAIL (or, without an email, when there are no
    # users at all). An existing account is never changed - passwords are managed in Admin -> Users after that.
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    admin_name: str = os.getenv("ADMIN_NAME", "Administrator")
    admin_email: str = os.getenv("ADMIN_EMAIL", "").strip().lower()

    # India Standard Time by default; dispatch dates are bucketed in this offset.
    tz_offset_minutes: int = _int("TZ_OFFSET_MINUTES", 330)
    # A dispatch "day" starts at this local hour (e.g. 6 => scans before 6am count for the previous day).
    day_start_hour: int = min(23, max(0, _int("DAY_START_HOUR", 0)))

    cors_origins: str = os.getenv("CORS_ORIGINS", "")

    # --- Android app distribution (routers/mobile_app.py) ---
    # Public address of this server, e.g. https://scan.youthnic.shop - used for the download link / QR code.
    # Empty = taken from the request (X-Forwarded-* headers behind a proxy).
    public_url: str = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    # Where the APK build workflow drops forward-scan-app.apk + latest.json (data/ is kept across deploys).
    app_release_dir: str = field(default_factory=lambda: _path_setting("APP_RELEASE_DIR", "data/app"))


settings = Settings()
