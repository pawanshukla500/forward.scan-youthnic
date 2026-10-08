from __future__ import annotations

import threading
import time

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import MobileDeviceSession, User
from ..security import (
    COOKIE_NAME,
    MOBILE_REFRESH_DAYS,
    current_user,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    issue_token,
    password_problem,
    verify_password,
)
from ..timeutil import iso_utc, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])

# In-memory brute-force guard, per 10 minutes: 8 failed attempts per sign-in id + IP, and 30 per sign-in id from
# anywhere (rotating IPs). Attempts are counted BEFORE the password is checked, so parallel guesses all count.
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()
MAX_FAILURES = 8
MAX_ACCOUNT_FAILURES = 30
FAILURE_WINDOW = 600
_dummy_hash: str | None = None


class LoginIn(BaseModel):
    username: str = Field(max_length=200)  # username or email
    password: str = Field(max_length=200)
    remember: bool = True  # False = session cookie, gone when the browser closes (shared stations)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


def user_payload(u: User) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "email": u.email, "role": u.role,
            "must_change_password": bool(u.must_change_password), "password_changed_at": iso_utc(u.password_changed_at)}


def _attempt(ident: str, request: Request, kind: str = "") -> tuple[str, str]:
    """Count one sign-in attempt for this id + IP and for the id, or refuse with 429. Returns the keys."""
    ip = request.client.host if request.client else ""
    keys = (f"{kind}{ident}|{ip}", f"acct|{ident}")
    now = time.time()
    with _failures_lock:
        if len(_failures) > 5000:  # forget expired entries (random user names would otherwise pile up)
            for k in [k for k, ts in _failures.items() if not ts or now - ts[-1] >= FAILURE_WINDOW]:
                _failures.pop(k, None)
            if len(_failures) > 20000:  # a flood of new names: keep the newest only
                for k in sorted(_failures, key=lambda k: _failures[k][-1] if _failures[k] else 0)[:10000]:
                    _failures.pop(k, None)
        for key, cap in zip(keys, (MAX_FAILURES, MAX_ACCOUNT_FAILURES)):
            recent = [t for t in _failures.get(key, []) if now - t < FAILURE_WINDOW]
            if len(recent) >= cap:
                raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
            _failures[key] = recent
        for key in keys:
            _failures[key].append(now)
    return keys


def _signed_in(keys: tuple[str, str]) -> None:
    with _failures_lock:
        for key in keys:
            _failures.pop(key, None)


def _password_ok(user: User | None, password: str) -> bool:
    """bcrypt also for an unknown / disabled name: the answer time must not say whether the name exists."""
    global _dummy_hash
    if user is None or not user.is_active:
        if _dummy_hash is None:
            _dummy_hash = hash_password("not-a-real-password-0")
        verify_password(password, _dummy_hash)
        return False
    return verify_password(password, user.password_hash)


def _set_session(response: Response, request: Request, user: User, remember: bool) -> str:
    token = issue_token(user)
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=settings.token_hours * 3600 if remember else None,
        secure=request.url.scheme == "https",
    )
    return token


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ident = body.username.strip().lower()
    keys = _attempt(ident, request)
    # one field for both: "pawan.shukla" or "returnorders@vbexports.co.in"
    who = User.email == ident if "@" in ident else User.username == ident
    user = db.scalar(select(User).where(who)) if ident else None
    if not _password_ok(user, body.password):
        raise HTTPException(401, "Wrong email / username or password")
    _signed_in(keys)
    user.last_login_at = utcnow()
    db.commit()
    token = _set_session(response, request, user, body.remember)
    return {"user": user_payload(user), "token": token}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"user": user_payload(user)}


@router.post("/change-password")
def change_password(body: PasswordChangeIn, request: Request, response: Response, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Anyone signed in sets their own password (required after an admin created or reset the account).
    Other sessions of this account are signed out; this one stays signed in with a new session."""
    keys = _attempt(f"user#{user.id}", request, "change-password|")
    user = db.get(User, user.id)
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(401, "Your current password is not right")
    if body.new_password == body.current_password:
        raise HTTPException(400, "Choose a password different from the current one")
    if problem := password_problem(body.new_password, username=user.username, email=user.email):
        raise HTTPException(400, problem)
    _signed_in(keys)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.password_changed_at = utcnow()
    user.token_version += 1
    db.commit()
    _set_session(response, request, user, remember=True)
    return {"user": user_payload(user)}


# ---- mobile device sessions (long-lived 90-day refresh) -----------------------------------


class MobileLoginIn(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=200)
    device_info: str = Field(default="", max_length=200)


class MobileRefreshIn(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=256)
    device_info: str = Field(default="", max_length=200)


class MobileLogoutIn(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=256)


@router.post("/mobile/login")
def mobile_login(body: MobileLoginIn, request: Request, db: Session = Depends(get_db)):
    """Issue a normal short-lived access JWT plus a 90-day cryptographically random refresh token."""
    ident = body.username.strip().lower()
    keys = _attempt(ident, request, "mobile|")
    who = User.email == ident if "@" in ident else User.username == ident
    user = db.scalar(select(User).where(who)) if ident else None
    if not _password_ok(user, body.password):
        raise HTTPException(401, "Wrong email / username or password")
    _signed_in(keys)
    now = utcnow()
    user.last_login_at = now

    raw_refresh = generate_refresh_token()
    session = MobileDeviceSession(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        device_info=body.device_info.strip()[:200],
        token_version=user.token_version,
        created_at=now,
        expires_at=now + timedelta(days=MOBILE_REFRESH_DAYS),
        last_used_at=now,
    )
    db.add(session)
    db.commit()

    token = issue_token(user)
    return {
        "user": user_payload(user),
        "token": token,
        "refresh_token": raw_refresh,
        "expires_at": iso_utc(now + timedelta(hours=settings.token_hours)),
        "refresh_expires_at": iso_utc(session.expires_at),
    }


def _revoke_chain(db: Session, session_id: int | None, now) -> None:
    """Revoke a session and every session that replaced it (rotation chain)."""
    seen: set[int] = set()
    while session_id and session_id not in seen:
        seen.add(session_id)
        s = db.get(MobileDeviceSession, session_id)
        if s is None:
            break
        if s.revoked_at is None:
            s.revoked_at = now
        session_id = s.replaced_by_id


@router.post("/mobile/refresh")
def mobile_refresh(body: MobileRefreshIn, db: Session = Depends(get_db)):
    """Exchange a valid mobile refresh token for a fresh access token and rotate the refresh token.

    Rotated tokens: if the phone never received the answer (timeout, app killed) it still holds the old token. That
    old token is accepted once more as long as the token that replaced it was never used - the phone is not signed
    out mid-shift. An old token coming back AFTER its replacement was used means a copy is around: that whole sign-in
    chain is ended (sign in again)."""
    thash = hash_refresh_token(body.refresh_token)
    session = db.scalar(select(MobileDeviceSession).where(MobileDeviceSession.token_hash == thash))
    if not session:
        raise HTTPException(401, "Session is invalid or has been revoked")
    now = utcnow()
    nxt = None  # set = the answer of an earlier renewal was lost: its unused new token is replaced again
    if session.revoked_at is not None:
        nxt = db.get(MobileDeviceSession, session.replaced_by_id) if session.replaced_by_id else None
        if nxt is None:
            raise HTTPException(401, "Session is invalid or has been revoked")  # signed out / disabled
        if nxt.revoked_at is not None or nxt.replaced_by_id is not None:
            _revoke_chain(db, nxt.id, now)
            db.commit()
            raise HTTPException(401, "This sign-in was renewed on another device. Please sign in again.")

    if session.expires_at <= now:
        raise HTTPException(401, "Refresh session has expired. Please sign in again.")

    user = session.user
    if not user or not user.is_active:
        session.revoked_at = now
        db.commit()
        raise HTTPException(401, "User account is disabled")

    if user.token_version != session.token_version:
        session.revoked_at = now
        db.commit()
        raise HTTPException(401, "Password has changed or session was invalidated. Please sign in again.")

    new_raw_refresh = generate_refresh_token()
    new_session = MobileDeviceSession(
        user_id=user.id,
        token_hash=hash_refresh_token(new_raw_refresh),
        device_info=(body.device_info.strip() or session.device_info)[:200],
        token_version=user.token_version,
        created_at=now,
        expires_at=now + timedelta(days=MOBILE_REFRESH_DAYS),
        last_used_at=now,
    )
    db.add(new_session)
    db.flush()
    if nxt is not None:
        # one live token per sign-in: the never-delivered one is retired and points at the new one
        nxt.revoked_at = now
        nxt.replaced_by_id = new_session.id
        session.replaced_by_id = new_session.id
        session.last_used_at = now
    else:
        # Rotate atomically: only one request turns this token into "replaced"; a parallel one with the same token
        # (the phone never sends two at once) is refused instead of forking an unlinked second chain.
        res = db.execute(update(MobileDeviceSession)
                         .where(MobileDeviceSession.id == session.id, MobileDeviceSession.revoked_at.is_(None))
                         .values(revoked_at=now, last_used_at=now, replaced_by_id=new_session.id)
                         .execution_options(synchronize_session=False))
        if res.rowcount != 1:
            db.rollback()
            raise HTTPException(401, "Session is invalid or has been revoked")
    db.commit()

    token = issue_token(user)
    return {
        "user": user_payload(user),
        "token": token,
        "refresh_token": new_raw_refresh,
        "expires_at": iso_utc(now + timedelta(hours=settings.token_hours)),
        "refresh_expires_at": iso_utc(new_session.expires_at),
    }


@router.post("/mobile/logout")
def mobile_logout(body: MobileLogoutIn = MobileLogoutIn(), db: Session = Depends(get_db)):
    """Revoke a mobile device session so its refresh token cannot be used again."""
    if body.refresh_token:
        thash = hash_refresh_token(body.refresh_token)
        session = db.scalar(select(MobileDeviceSession).where(MobileDeviceSession.token_hash == thash))
        if session and not session.revoked_at:
            session.revoked_at = utcnow()
            db.commit()
    return {"ok": True}

