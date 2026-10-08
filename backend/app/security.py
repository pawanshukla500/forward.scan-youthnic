from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, WebSocket, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User
from .timeutil import utcnow

COOKIE_NAME = "fs_session"
_ALGO = "HS256"


def _secret() -> str:
    if not settings.secret_key:
        raise RuntimeError("APP_SECRET_KEY is not set in .env")
    return settings.secret_key


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


MOBILE_REFRESH_DAYS = 90


def generate_refresh_token() -> str:
    """Cryptographically secure high-entropy random token for mobile device sessions."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """SHA-256 hash of the refresh token stored in the database."""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()



PASSWORD_RULE = "at least 8 characters, with a letter and a number"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def password_problem(password: str, *, username: str = "", email: str = "") -> str | None:
    """Why this password is not acceptable, or None. bcrypt only reads the first 72 bytes, hence the cap."""
    if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return f"Password must be {PASSWORD_RULE}"
    if len(password.encode("utf-8")) > 72:
        return "Password must be at most 72 characters"
    low = password.lower()
    if username and low == username.lower() or email and low in (email.lower(), email.split("@")[0].lower()):
        return "Password must not be the username or email"
    return None


def normalize_email(email: str | None) -> str:
    """Lower-case email, or "" when none; raises ValueError for something that is not an email address."""
    value = (email or "").strip().lower()
    if value and not _EMAIL.match(value):
        raise ValueError("Enter a valid email address")
    return value


def issue_token(user: User) -> str:
    now = utcnow()
    payload = {
        "sub": str(user.id),
        "ver": user.token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.token_hours)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGO)


def _user_from_token(token: str | None, db: Session) -> User | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGO])
    except jwt.PyJWTError:
        return None
    user = db.get(User, int(payload.get("sub", 0)))
    if not user or not user.is_active or user.token_version != payload.get("ver"):
        return None
    return user


def _token_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = _user_from_token(_token_from_request(request), db)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    if user.must_change_password and not request.url.path.startswith("/api/auth/"):
        # an admin set this password: nothing else works until the person has chosen their own
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Choose a new password first")
    # Hand the connection back while the request waits for a worker thread (under full CPU, waiting requests held
    # every pooled connection); the session takes one again on its next query and the user stays loaded.
    db.commit()
    return user


def websocket_user(ws: WebSocket, db: Session) -> User | None:
    user = _user_from_token(ws.cookies.get(COOKIE_NAME) or ws.query_params.get("token"), db)
    return None if user is None or user.must_change_password else user


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have permission for this action")
        return user

    return dep


# admin, manager and supervisor (models.STAFF_ROLES)
require_supervisor = require_role("admin", "manager", "supervisor")
require_admin = require_role("admin")
