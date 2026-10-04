"""Installed-app auth: the login JWT works as an Authorization Bearer token (no cookies),
which is how the Android APK signs in and stays signed in after install."""
from fastapi.testclient import TestClient

from app.main import app

ADMIN_PW = "test-admin-pass"


def bearer_client():
    """A client with an empty cookie jar that authenticates purely by Bearer token - like the app."""
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert token
    c.cookies.clear()  # no cookies leave this device: token only
    c.headers["Authorization"] = f"Bearer {token}"
    return c, token


def test_login_returns_a_bearer_token():
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 200, r.text
        assert r.json()["token"]


def test_bearer_token_authenticates_without_cookies():
    c, _ = bearer_client()
    me = c.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["user"]["username"] == "admin"
    channels = c.get("/api/channels")
    assert channels.status_code == 200, channels.text


def test_bad_bearer_token_is_rejected():
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer this-is-not-a-token"
        assert c.get("/api/auth/me").status_code == 401
        assert c.get("/api/channels").status_code == 401


def test_password_change_kills_old_app_tokens():
    with TestClient(app) as admin:
        assert admin.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW}).status_code == 200
        assert admin.post("/api/admin/users", json={"username": "app.user", "full_name": "App User",
                                                    "password": "Start2026go", "role": "scanner",
                                                    "must_change_password": False}).status_code == 200
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": "app.user", "password": "Start2026go"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    c.cookies.clear()
    c.headers["Authorization"] = f"Bearer {token}"
    assert c.post("/api/auth/change-password",
                  json={"current_password": "Start2026go", "new_password": "App2026token1"}).status_code == 200
    stale = TestClient(app)
    stale.headers["Authorization"] = f"Bearer {token}"
    assert stale.get("/api/auth/me").status_code == 401  # token_version bumped: old app session is out
