from __future__ import annotations

import time

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
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

# Simple in-memory brute-force guard: max 8 failed attempts per sign-in id (or account) + IP per 10 minutes.
_failures: dict[str, list[float]] = {}
MAX_FAILURES = 8
FAILURE_WINDOW = 600


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


def _throttle(key: str) -> list[float]:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < FAILURE_WINDOW]
    if len(recent) >= MAX_FAILURES:
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    return recent


def _fail(key: str, recent: list[float], message: str) -> HTTPException:
    recent.append(time.time())
    _failures[key] = recent
    return HTTPException(401, message)


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
    key = f"{ident}|{request.client.host if request.client else ''}"
    recent = _throttle(key)
    # one field for both: "pawan.shukla" or "returnorders@vbexports.co.in"
    who = User.email == ident if "@" in ident else User.username == ident
    user = db.scalar(select(User).where(who)) if ident else None
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        raise _fail(key, recent, "Wrong email / username or password")
    _failures.pop(key, None)
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
    key = f"change-password|{user.id}|{request.client.host if request.client else ''}"
    recent = _throttle(key)
    user = db.get(User, user.id)
    if not verify_password(body.current_password, user.password_hash):
        raise _fail(key, recent, "Your current password is not right")
    if body.new_password == body.current_password:
        raise HTTPException(400, "Choose a password different from the current one")
    if problem := password_problem(body.new_password, username=user.username, email=user.email):
        raise HTTPException(400, problem)
    _failures.pop(key, None)
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
    key = f"mobile|{ident}|{request.client.host if request.client else ''}"
    recent = _throttle(key)
    who = User.email == ident if "@" in ident else User.username == ident
    user = db.scalar(select(User).where(who)) if ident else None
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        raise _fail(key, recent, "Wrong email / username or password")
    _failures.pop(key, None)
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


@router.post("/mobile/refresh")
def mobile_refresh(body: MobileRefreshIn, db: Session = Depends(get_db)):
    """Exchange a valid mobile refresh token for a fresh access token and rotate the refresh token."""
    thash = hash_refresh_token(body.refresh_token)
    session = db.scalar(select(MobileDeviceSession).where(MobileDeviceSession.token_hash == thash))
    if not session or session.revoked_at is not None:
        raise HTTPException(401, "Session is invalid or has been revoked")

    now = utcnow()
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

    # Rotate refresh token: revoke previous session, issue new one
    session.revoked_at = now
    session.last_used_at = now

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

