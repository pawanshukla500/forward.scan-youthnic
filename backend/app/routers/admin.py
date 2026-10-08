from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ROLES, Channel, OmsOrder, User, Warehouse
from ..oms.mapping import ChannelMatcher
from ..oms.sync import _set_state, get_engine
from ..security import hash_password, normalize_email, password_problem, require_admin, require_supervisor
from ..services import backup, cache, tracking
from ..timeutil import iso_utc, today_dispatch_date, utcnow
from .scan import channel_payload

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---- users ---------------------------------------------------------------------------------


def _user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "email": u.email, "role": u.role,
            "is_active": u.is_active, "must_change_password": bool(u.must_change_password),
            "created_at": iso_utc(u.created_at), "last_login_at": iso_utc(u.last_login_at),
            "password_changed_at": iso_utc(u.password_changed_at)}


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    full_name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200)
    password: str = Field(max_length=200)
    role: str = "scanner"
    # the person picks their own password at first sign-in (on by default: the admin knows this one)
    must_change_password: bool = True


class UserPatch(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=200)  # admin reset
    must_change_password: bool | None = None  # with a reset: ask for a new one at next sign-in (default yes)
    role: str | None = None
    is_active: bool | None = None


def _email_or_400(db: Session, email: str | None, user_id: int | None = None) -> str:
    try:
        value = normalize_email(email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if value:
        clash = db.scalar(select(User.id).where(User.email == value, User.id != (user_id or 0)))
        if clash:
            raise HTTPException(400, "Another user already has this email")
    return value


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(require_supervisor)):
    return {"users": [_user_out(u) for u in db.scalars(select(User).order_by(User.full_name, User.username))]}


# Managers and supervisors look after scanner accounts only: create them, reset a forgotten password, disable
# someone who left. Everything else about users (other roles, names, emails, role changes) stays with admins.
SCANNER_ONLY = "Managers and supervisors can only manage scanner accounts - ask an admin"


def _may_manage(me: User, target_role: str) -> None:
    if me.role != "admin" and target_role != "scanner":
        raise HTTPException(403, SCANNER_ONLY)


@router.post("/users")
def create_user(body: UserIn, db: Session = Depends(get_db), me: User = Depends(require_supervisor)):
    if body.role not in ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(ROLES)}")
    _may_manage(me, body.role)
    uname = body.username.strip().lower()
    if db.scalar(select(User).where(User.username == uname)):
        raise HTTPException(400, "Username already exists")
    email = _email_or_400(db, body.email)
    if problem := password_problem(body.password, username=uname, email=email):
        raise HTTPException(400, problem)
    u = User(username=uname, full_name=body.full_name.strip(), email=email, password_hash=hash_password(body.password),
             role=body.role, must_change_password=body.must_change_password, password_changed_at=utcnow())
    db.add(u)
    db.commit()
    return {"user": _user_out(u)}


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, db: Session = Depends(get_db), me: User = Depends(require_supervisor)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    if me.role != "admin":
        _may_manage(me, u.role)
        if body.role is not None or body.full_name is not None or body.email is not None:
            raise HTTPException(403, "Managers and supervisors can reset a scanner's password or disable it - "
                                     "ask an admin to change names, emails or roles")
    was_active_admin = u.role == "admin" and u.is_active
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(400, f"Role must be one of {', '.join(ROLES)}")
        if u.id == me.id and body.role != "admin":
            raise HTTPException(400, "You cannot remove your own admin role")
        u.role = body.role
    if body.full_name is not None:
        u.full_name = body.full_name.strip()
    if body.email is not None:
        u.email = _email_or_400(db, body.email, u.id)
    if body.password is not None:
        if problem := password_problem(body.password, username=u.username, email=u.email):
            raise HTTPException(400, problem)
        u.password_hash = hash_password(body.password)
        u.password_changed_at = utcnow()
        u.must_change_password = True if body.must_change_password is None else body.must_change_password
        u.token_version += 1  # sign out existing sessions
    elif body.must_change_password is not None:
        u.must_change_password = body.must_change_password
    if body.is_active is not None:
        if u.id == me.id and not body.is_active:
            raise HTTPException(400, "You cannot deactivate yourself")
        u.is_active = body.is_active
        if not body.is_active:
            u.token_version += 1
    if was_active_admin and not (u.role == "admin" and u.is_active) and _active_admins(db) <= 1:
        db.rollback()
        raise HTTPException(400, "Keep at least one active admin")
    db.commit()
    return {"user": _user_out(u)}


# ---- channels ------------------------------------------------------------------------------


class ChannelPatch(BaseModel):
    scan_enabled: bool | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    sort_order: int | None = None
    aliases: str | None = Field(default=None, max_length=2000)


@router.get("/channels")
def admin_channels(db: Session = Depends(get_db), _: User = Depends(require_supervisor)):
    counts = dict(db.execute(select(OmsOrder.channel_id, func.count(OmsOrder.id)).group_by(OmsOrder.channel_id)).all())
    chans = db.scalars(select(Channel).order_by(Channel.sort_order, Channel.name))
    return {"channels": [{**channel_payload(c), "aliases": c.aliases, "cached_orders": counts.get(c.id, 0)} for c in chans]}


@router.patch("/channels/{channel_id}")
def patch_channel(channel_id: int, body: ChannelPatch, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    c = db.get(Channel, channel_id)
    if not c:
        raise HTTPException(404, "Channel not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    db.commit()
    remapped = _remap_unmapped(db) if body.aliases is not None else 0
    return {"channel": channel_payload(c), "remapped_orders": remapped}


def _remap_unmapped(db: Session) -> int:
    matcher = ChannelMatcher(db.scalars(select(Channel)))
    n = 0
    for o in db.scalars(select(OmsOrder).where(OmsOrder.channel_id.is_(None))):
        cid = matcher.match(o.channel_label, o.company)
        if cid:
            o.channel_id = cid
            n += 1
    db.commit()
    return n


@router.get("/unmapped-labels")
def unmapped_labels(db: Session = Depends(get_db), _: User = Depends(require_supervisor)):
    rows = db.execute(
        select(OmsOrder.channel_label, OmsOrder.company, func.count(OmsOrder.id))
        .where(OmsOrder.channel_id.is_(None))
        .group_by(OmsOrder.channel_label, OmsOrder.company)
        .order_by(func.count(OmsOrder.id).desc())
    ).all()
    return {"labels": [{"label": l, "company": c, "orders": n} for l, c, n in rows]}


# ---- warehouses ----------------------------------------------------------------------------


class WarehouseIn(BaseModel):
    id: int
    name: str = ""
    alias: str = ""
    sync_enabled: bool = True


@router.get("/warehouses")
def list_warehouses(db: Session = Depends(get_db), _: User = Depends(require_supervisor)):
    return {"warehouses": [{"id": w.id, "name": w.name, "alias": w.alias, "sync_enabled": w.sync_enabled}
                           for w in db.scalars(select(Warehouse).order_by(Warehouse.id))]}


@router.post("/warehouses")
def upsert_warehouse(body: WarehouseIn, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    w = db.get(Warehouse, body.id) or Warehouse(id=body.id)
    w.name, w.alias, w.sync_enabled = body.name, body.alias, body.sync_enabled
    db.add(w)
    db.commit()
    _set_state("warehouses_mode", "manual")  # admin choice wins over auto-selection from now on
    return {"ok": True}


# ---- counting start date ---------------------------------------------------------------------


class TrackingStartIn(BaseModel):
    date: date_type | None = None


@router.get("/tracking-start")
def get_tracking_start(_: User = Depends(require_supervisor)):
    d = tracking.start_date()
    return {"date": d.isoformat() if d else None, "today": today_dispatch_date().isoformat()}


@router.put("/tracking-start")
def put_tracking_start(body: TrackingStartIn, _: User = Depends(require_admin)):
    """Orders count from this dispatch day: earlier AWBs are not pending / overdue / reconciled."""
    if body.date and body.date > today_dispatch_date():
        raise HTTPException(400, "The start date cannot be in the future")
    tracking.set_start(body.date)
    # fill the days from the new date (within the kept window) on spare API credits; refresh every page's numbers
    _set_state("history_backfill", None)
    _set_state("history_done", False)
    cache.clear()
    d = tracking.start_date()
    return {"date": d.isoformat() if d else None, "today": today_dispatch_date().isoformat()}


# ---- backups -------------------------------------------------------------------------------


@router.get("/backups")
def backup_status(_: User = Depends(require_supervisor)):
    return backup.status()


@router.post("/backups/{kind}")
def backup_now(kind: str, _: User = Depends(require_admin)):
    if kind not in ("full", "recent"):
        raise HTTPException(400, "kind must be full or recent")
    if not backup.enabled():
        raise HTTPException(400, "Automatic backups are off (BACKUP_ENABLED=false)")
    backup.request(kind)
    return {"ok": True, "queued": kind}


# ---- sync ----------------------------------------------------------------------------------


@router.get("/sync")
def sync_status(_: User = Depends(require_supervisor)):
    eng = get_engine()
    if not eng:
        return {"enabled": False}
    return eng.status()


@router.post("/sync/{job}")
def sync_trigger(job: str, _: User = Depends(require_supervisor)):
    eng = get_engine()
    if not eng:
        raise HTTPException(400, "Sync is disabled (SYNC_ENABLED=false)")
    if job == "invoices":
        eng._last_urgent = 0  # noqa: SLF001 - manual trigger bypasses debounce
        eng.request_urgent()
    elif job in ("open_orders", "cancel_sweep", "channels", "crosscheck", "exit_check", "cleanup", "history"):
        eng.request_full(job)
    else:
        raise HTTPException(400, "Unknown job")
    return {"ok": True, "queued": job}
