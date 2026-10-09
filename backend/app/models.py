from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import event
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from .db import Base
from .timeutil import utcnow

ROLES = ("admin", "manager", "supervisor", "scanner")
# Reports, exports, removing scans - and scanner accounts: create, reset password, disable (admin-only otherwise).
STAFF_ROLES = ("admin", "manager", "supervisor")
# A scan made by the one-time "already shipped in OMSGuru" mark (services/marking.py), not by a packer. It counts as
# scanned (the packet is gone); a real scan of the same AWB later replaces it instead of answering "Duplicate".
MARKED_SHIPPED_FLAG = "MARKED_SHIPPED"
SYSTEM_SHIPPED_USERNAME = "omsguru.shipped"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    # optional sign-in id besides the username; lower case, unique when set (checked in the admin API)
    email: Mapped[str] = mapped_column(String(200), default="", index=True)
    password_hash: Mapped[str] = mapped_column(String(200))  # bcrypt, never the password itself
    role: Mapped[str] = mapped_column(String(20), default="scanner")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # bumped on password change / reset / disable: every session issued before is signed out
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    # set when an admin creates the account or resets its password: the person picks their own at next sign-in
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MobileDeviceSession(Base):
    """Long-lived device sessions for native mobile warehouse phones (90-day rolling refresh)."""

    __tablename__ = "mobile_device_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    device_info: Mapped[str] = mapped_column(String(200), default="")
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # set when this refresh token was rotated: the session that replaced it (reuse detection / lost-answer grace)
    replaced_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(lazy="joined")



class Channel(Base):
    """An OMSGuru channel company, i.e. a Sales Channel (seller account on a marketplace)."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200))          # "VB EXPORT - Myntra PPMP"
    marketplace: Mapped[str] = mapped_column(String(120))   # "Myntra PPMP"
    company: Mapped[str] = mapped_column(String(120), default="")
    oms_status: Mapped[str] = mapped_column(String(20), default="active")
    scan_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    color: Mapped[str] = mapped_column(String(16), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    # Extra spellings that appear in an order's "channel" field, one per line.
    aliases: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Warehouse(Base):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    alias: Mapped[str] = mapped_column(String(64), default="")
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class SkuPhoto(Base):
    """OMSGuru SKU catalogue photos, fetched from channel listings and order lines."""

    __tablename__ = "sku_photos"

    sku: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    image_url: Mapped[str] = mapped_column(Text, default="")
    marketplace: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OmsOrder(Base):
    """OMSGuru orders indexed by AWB.

    Unscanned orders that leave Packed / Ready-to-ship are kept for RETAIN_ORDERS_DAYS (7 days).
    Orders that were SCANNED are preserved for long-term history in PostgreSQL (SCANNED_ORDERS_RETENTION_DAYS,
    default 550 days / ~1.5 years) with active relations to scans.
    """

    __tablename__ = "oms_orders"
    # per-channel AWB-date ranges (scan station context, reconciliation by channel)
    __table_args__ = (Index("ix_oms_orders_channel_awb", "channel_id", "awb_generated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    oms_key: Mapped[str] = mapped_column(String(300), unique=True)
    oms_last_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_label: Mapped[str] = mapped_column(String(200), default="")
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True)
    company: Mapped[str] = mapped_column(String(120), default="")
    warehouse: Mapped[str] = mapped_column(String(64), default="")
    channel_order_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    sub_order_ids: Mapped[str] = mapped_column(Text, default="")
    invoice_id: Mapped[str] = mapped_column(String(120), default="")
    invoice_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    order_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sla_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shipment_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tracking_raw: Mapped[str] = mapped_column(String(120), default="")
    tracking_norm: Mapped[str] = mapped_column(String(120), default="", index=True)
    shipping_company: Mapped[str] = mapped_column(String(120), default="")
    order_type: Mapped[str] = mapped_column(String(40), default="")
    buyer_name: Mapped[str] = mapped_column(String(200), default="")
    buyer_city: Mapped[str] = mapped_column(String(120), default="")
    buyer_state: Mapped[str] = mapped_column(String(120), default="")
    buyer_pincode: Mapped[str] = mapped_column(String(20), default="")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    total_qty: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    status_text: Mapped[str] = mapped_column(String(200), default="")
    # OPEN | NOT_PACKED | CANCELLED | PARTIAL_CANCEL | SHIPPED | RETURN | MOVED | UNKNOWN
    status_group: Mapped[str] = mapped_column(String(20), default="UNKNOWN", index=True)
    source: Mapped[str] = mapped_column(String(20), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seen_open_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When the order stopped being Packed / Ready-to-ship in OMS.
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # When the AWB / shipping label was generated (OMSGuru invoice date). Drives "pending" and retention.
    awb_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Last time OMSGuru was asked what an unscanned order became after it left Packed / Ready-to-ship.
    exit_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # last time the hourly order-trail audit saw this AWB in OMSGuru's own invoice list (sync.step_audit)
    audit_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    channel: Mapped[Channel | None] = relationship(lazy="joined")


class Manifest(Base):
    """A dispatch batch: all scans for one sales channel on one business day (until closed)."""

    __tablename__ = "manifests"
    __table_args__ = (UniqueConstraint("dispatch_date", "channel_id", "seq", name="uq_manifest_day_channel_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_date: Mapped[date] = mapped_column(Date, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")  # OPEN | CLOSED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    channel: Mapped[Channel] = relationship(lazy="joined")


class Scan(Base):
    """One accepted shipment. tracking_norm is globally unique, which is what blocks duplicates."""

    __tablename__ = "scans"
    # Composite indexes matched to the real queries (measured on 1-3 years of synthetic scans, 3 Oct 2026):
    # every "per day [per channel] [per result]" count, and per-operator / hourly reports. They replace
    # single-column indexes on dispatch_date / user_id / result / scanned_at that the planner misused
    # (operators report: 191 s -> 36 ms on 3 years of data).
    __table_args__ = (
        Index("ix_scans_day_channel_result", "dispatch_date", "channel_id", "result", "alert"),
        Index("ix_scans_day_user_time", "dispatch_date", "user_id", "scanned_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tracking_norm: Mapped[str] = mapped_column(String(120), unique=True)
    tracking_raw: Mapped[str] = mapped_column(String(160))
    dispatch_date: Mapped[date] = mapped_column(Date)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    station: Mapped[str] = mapped_column(String(60), default="")
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("oms_orders.id"), nullable=True, index=True)
    manifest_id: Mapped[int | None] = mapped_column(ForeignKey("manifests.id"), nullable=True, index=True)
    # OK | WARN | UNVERIFIED
    result: Mapped[str] = mapped_column(String(12), default="OK")
    flags: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(String(300), default="")
    # Raised by background sync after the scan, e.g. the order was cancelled later.
    alert: Mapped[str] = mapped_column(String(300), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # OMSGuru dispatch update: PENDING | EXPORTED | DONE | FAILED
    oms_update_status: Mapped[str] = mapped_column(String(10), default="PENDING")
    oms_update_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    oms_update_note: Mapped[str] = mapped_column(String(300), default="")
    # Copy of the order details at scan time (kept forever; the order working set is pruned).
    order_json: Mapped[str] = mapped_column(Text, default="")

    user: Mapped[User] = relationship(lazy="joined")
    channel: Mapped[Channel] = relationship(lazy="joined")
    order: Mapped[OmsOrder | None] = relationship(lazy="joined")
    manifest: Mapped[Manifest | None] = relationship(lazy="joined")


class ScanEvent(Base):
    """Audit trail of every scan attempt, including rejected ones (duplicates, wrong channel...)."""

    __tablename__ = "scan_events"
    # rejected-scan counts per day / channel / outcome (dashboard, scan station, channel summary)
    __table_args__ = (Index("ix_scan_events_day_channel_outcome", "dispatch_date", "channel_id", "outcome"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    dispatch_date: Mapped[date] = mapped_column(Date)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    station: Mapped[str] = mapped_column(String(60), default="")
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True)
    tracking_raw: Mapped[str] = mapped_column(String(160), default="")
    tracking_norm: Mapped[str] = mapped_column(String(120), default="")
    # ACCEPTED | WARN | UNVERIFIED | DUPLICATE | WRONG_CHANNEL | BLOCKED | INVALID | VOIDED
    outcome: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(String(300), default="")
    scan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User | None] = relationship(lazy="joined")


class DeletedScan(Base):
    """Every scan the app removes (a supervisor's "remove scan", a real scan replacing a one-time mark, a clean-up
    script, any future code) is copied here first - the whole row, with why and when - so scanned data is never lost.
    backend/restore_deleted_scans.py puts one back. Only the SCAN_RETENTION_DAYS clean-up (3 years) is not copied."""

    __tablename__ = "deleted_scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(Integer, index=True)
    tracking_norm: Mapped[str] = mapped_column(String(120), index=True)
    dispatch_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    data: Mapped[str] = mapped_column(Text, default="")  # JSON of every column of the scan


def _jsonable(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


@event.listens_for(Session, "before_flush")
def _archive_deleted_scans(session, flush_context, instances) -> None:
    """Runs for EVERY session before rows are written: scans about to be deleted are copied to deleted_scans in the
    same transaction (rolled back together if the delete is). The caller may say why in session.info["delete_reason"]."""
    import json

    for obj in list(session.deleted):
        if isinstance(obj, Scan):
            session.add(DeletedScan(
                scan_id=obj.id, tracking_norm=obj.tracking_norm or "", dispatch_date=obj.dispatch_date,
                channel_id=obj.channel_id, deleted_at=utcnow(),
                reason=str(session.info.get("delete_reason") or "removed")[:300],
                data=json.dumps({c.name: _jsonable(getattr(obj, c.name)) for c in Scan.__table__.columns}),
            ))


class SyncState(Base):
    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    records: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
