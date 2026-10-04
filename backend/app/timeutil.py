"""Time helpers. All timestamps are stored in UTC; dispatch dates use the configured local offset."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .config import settings

LOCAL_TZ = timezone(timedelta(minutes=settings.tz_offset_minutes))


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def dispatch_date_for(dt_utc: datetime) -> date:
    """The business day a scan belongs to (honours DAY_START_HOUR)."""
    local = to_local(dt_utc) - timedelta(hours=settings.day_start_hour)
    return local.date()


def today_dispatch_date() -> date:
    return dispatch_date_for(utcnow())


def day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """UTC [start, end) of a business day."""
    start_local = datetime.combine(d, time(hour=settings.day_start_hour), tzinfo=LOCAL_TZ)
    start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def from_unix(ts) -> datetime | None:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def iso_utc(dt: datetime | None) -> str | None:
    return None if dt is None else dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
