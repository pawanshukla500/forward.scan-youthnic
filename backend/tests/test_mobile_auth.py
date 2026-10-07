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


def test_mobile_login_success():
    with TestClient(app) as c:
        r = c.post("/api/auth/mobile/login", json={"username": "admin", "password": ADMIN_PW, "device_info": "Phone A"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["user"]["username"] == "admin"
        assert data["token"]
        assert data["refresh_token"]
        assert data["expires_at"]
        assert data["refresh_expires_at"]

        # Verify access token works with Bearer auth
        c.headers["Authorization"] = f"Bearer {data['token']}"
        me = c.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "admin"


def test_mobile_refresh_rotates_token():
    with TestClient(app) as c:
        r = c.post("/api/auth/mobile/login", json={"username": "admin", "password": ADMIN_PW, "device_info": "Phone A"})
        assert r.status_code == 200
        orig_refresh = r.json()["refresh_token"]

        # Refresh
        r2 = c.post("/api/auth/mobile/refresh", json={"refresh_token": orig_refresh})
        assert r2.status_code == 200, r2.text
        data2 = r2.json()
        assert data2["token"]
        new_refresh = data2["refresh_token"]
        assert new_refresh != orig_refresh

        # New access token works
        c.headers["Authorization"] = f"Bearer {data2['token']}"
        assert c.get("/api/auth/me").status_code == 200

        # Old refresh token is revoked and cannot be used again
        r_old = c.post("/api/auth/mobile/refresh", json={"refresh_token": orig_refresh})
        assert r_old.status_code == 401

        # Second refresh with new token works
        r3 = c.post("/api/auth/mobile/refresh", json={"refresh_token": new_refresh})
        assert r3.status_code == 200


def test_mobile_expired_refresh_rejected():
    from datetime import datetime, timedelta
    from app.db import session_scope
    from app.models import MobileDeviceSession
    from app.security import hash_refresh_token

    with TestClient(app) as c:
        r = c.post("/api/auth/mobile/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 200
        ref_token = r.json()["refresh_token"]

        # Expire the session in database
        thash = hash_refresh_token(ref_token)
        with session_scope() as db:
            s = db.query(MobileDeviceSession).filter(MobileDeviceSession.token_hash == thash).first()
            assert s is not None
            s.expires_at = datetime.utcnow() - timedelta(days=1)

        r_exp = c.post("/api/auth/mobile/refresh", json={"refresh_token": ref_token})
        assert r_exp.status_code == 401
        assert "expired" in r_exp.json()["detail"].lower()


def test_mobile_disabled_user_rejected():
    from app.db import session_scope
    from app.models import User

    with TestClient(app) as admin:
        admin.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        admin.post("/api/admin/users", json={"username": "mobile.disabled", "full_name": "Disabled Mobile",
                                            "password": "Start2026go", "role": "scanner",
                                            "must_change_password": False})

    with TestClient(app) as c:
        r = c.post("/api/auth/mobile/login", json={"username": "mobile.disabled", "password": "Start2026go"})
        assert r.status_code == 200
        ref_token = r.json()["refresh_token"]

        # Disable the user
        with session_scope() as db:
            u = db.query(User).filter(User.username == "mobile.disabled").first()
            u.is_active = False

        r_dis = c.post("/api/auth/mobile/refresh", json={"refresh_token": ref_token})
        assert r_dis.status_code == 401


def test_mobile_password_change_invalidates_refresh():
    with TestClient(app) as admin:
        admin.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        admin.post("/api/admin/users", json={"username": "mobile.pwchange", "full_name": "PW Change Mobile",
                                            "password": "Start2026go", "role": "scanner",
                                            "must_change_password": False})

    with TestClient(app) as c:
        r = c.post("/api/auth/mobile/login", json={"username": "mobile.pwchange", "password": "Start2026go"})
        assert r.status_code == 200
        token = r.json()["token"]
        ref_token = r.json()["refresh_token"]

        # Change password using the access token
        c.headers["Authorization"] = f"Bearer {token}"
        r_change = c.post("/api/auth/change-password",
                          json={"current_password": "Start2026go", "new_password": "App2026token2"})
        assert r_change.status_code == 200

        # Previous mobile refresh token is now invalid due to token_version mismatch
        r_ref = c.post("/api/auth/mobile/refresh", json={"refresh_token": ref_token})
        assert r_ref.status_code == 401


def test_mobile_logout_revokes_session():
    with TestClient(app) as c:
        r = c.post("/api/auth/mobile/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 200
        ref_token = r.json()["refresh_token"]

        # Logout
        r_out = c.post("/api/auth/mobile/logout", json={"refresh_token": ref_token})
        assert r_out.status_code == 200

        # Cannot refresh anymore
        r_ref = c.post("/api/auth/mobile/refresh", json={"refresh_token": ref_token})
        assert r_ref.status_code == 401

