"""Users and sign-in: the default admin from .env, admin-managed accounts, roles, password set / reset / change."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.main import _bootstrap_admin, app
from app.models import User
from app.routers import auth as auth_router

ADMIN_PW = "test-admin-pass"


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW}).status_code == 200
        yield c


def other():
    return TestClient(app)  # its own cookie jar = another browser


def login(c, ident, password):
    return c.post("/api/auth/login", json={"username": ident, "password": password})


def test_default_admin_is_created_once_and_never_overwritten(admin):
    made = _bootstrap_admin(name="Pawan Test", email="Owner@Example.com", username="owner.test", password="Owner2026pass")
    assert made is not None
    with session_scope() as db:
        u = db.scalar(select(User).where(User.email == "owner@example.com"))
        assert u.role == "admin" and u.full_name == "Pawan Test" and u.username == "owner.test"
        assert u.password_hash != "Owner2026pass" and u.password_hash.startswith("$2")  # bcrypt, never plain text
        assert not u.must_change_password
    # next start: the account exists - nothing changes, even with another password in .env
    assert _bootstrap_admin(name="X", email="owner@example.com", username="x", password="Different99pass") is None
    c = other()
    assert login(c, "owner@example.com", "Owner2026pass").status_code == 200  # by email (any case)
    assert login(other(), "OWNER.TEST", "Owner2026pass").status_code == 200   # or by username
    assert login(other(), "owner@example.com", "Different99pass").status_code == 401


def test_admin_creates_a_user_who_must_choose_their_own_password(admin):
    r = admin.post("/api/admin/users", json={"username": "ravi.k", "full_name": "Ravi Kumar", "email": "ravi@example.com",
                                             "password": "Start2026go", "role": "scanner"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["must_change_password"] is True and r.json()["user"]["email"] == "ravi@example.com"

    ravi = other()
    me = login(ravi, "ravi@example.com", "Start2026go")
    assert me.status_code == 200 and me.json()["user"]["must_change_password"] is True
    blocked = ravi.get("/api/channels")
    assert blocked.status_code == 403 and "new password" in blocked.json()["detail"]
    assert ravi.get("/api/auth/me").status_code == 200

    assert ravi.post("/api/auth/change-password", json={"current_password": "wrong-one1", "new_password": "Ravi2026own"}).status_code == 401
    assert ravi.post("/api/auth/change-password", json={"current_password": "Start2026go", "new_password": "short1"}).status_code == 400
    assert ravi.post("/api/auth/change-password", json={"current_password": "Start2026go", "new_password": "ravi.k"}).status_code == 400
    ok = ravi.post("/api/auth/change-password", json={"current_password": "Start2026go", "new_password": "Ravi2026own"})
    assert ok.status_code == 200 and ok.json()["user"]["must_change_password"] is False
    assert ravi.get("/api/channels").status_code == 200  # this session carries on with the new cookie
    assert login(other(), "ravi.k", "Start2026go").status_code == 401
    assert login(other(), "ravi.k", "Ravi2026own").status_code == 200


def test_admin_reset_signs_the_person_out_and_asks_for_a_new_password(admin):
    admin.post("/api/admin/users", json={"username": "meena.s", "full_name": "Meena S", "password": "Meena2026a",
                                         "role": "scanner", "must_change_password": False})
    meena = other()
    assert login(meena, "meena.s", "Meena2026a").status_code == 200
    assert meena.get("/api/channels").status_code == 200
    uid = next(u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["username"] == "meena.s")

    assert admin.patch(f"/api/admin/users/{uid}", json={"password": "weak"}).status_code == 400
    r = admin.patch(f"/api/admin/users/{uid}", json={"password": "Reset2026b"})
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    assert meena.get("/api/channels").status_code == 401  # the old session is gone
    assert login(other(), "meena.s", "Meena2026a").status_code == 401
    assert login(meena, "meena.s", "Reset2026b").json()["user"]["must_change_password"] is True


def test_roles_emails_and_admin_only_actions(admin):
    r = admin.post("/api/admin/users", json={"username": "dup.mail", "email": "RAVI@example.com", "password": "Dup2026pass"})
    assert r.status_code == 400 and "email" in r.json()["detail"]
    assert admin.post("/api/admin/users", json={"username": "bad.mail", "email": "not-an-email", "password": "Bad2026pass"}).status_code == 400
    assert admin.post("/api/admin/users", json={"username": "ravi.k", "password": "Again2026x"}).status_code == 400

    lead = admin.post("/api/admin/users", json={"username": "lead.one", "password": "Lead2026one", "role": "supervisor",
                                                "must_change_password": False}).json()["user"]
    assert admin.patch(f"/api/admin/users/{lead['id']}", json={"role": "chief"}).status_code == 400
    assert admin.patch(f"/api/admin/users/{lead['id']}", json={"email": "lead@example.com", "full_name": "Lead One"}).json()["user"]["email"] == "lead@example.com"

    sup = other()
    assert login(sup, "lead@example.com", "Lead2026one").status_code == 200
    assert sup.get("/api/admin/users").status_code == 200  # supervisors can see the team
    # ...and add scanner IDs (test_managers_and_supervisors_manage_scanners_only), but nothing above that
    assert sup.post("/api/admin/users", json={"username": "x.y", "password": "Xy2026xyzz", "role": "supervisor"}).status_code == 403
    assert sup.patch(f"/api/admin/users/{lead['id']}", json={"role": "admin"}).status_code == 403

    me_id = next(u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["username"] == "admin")
    assert admin.patch(f"/api/admin/users/{me_id}", json={"role": "scanner"}).status_code == 400
    assert admin.patch(f"/api/admin/users/{me_id}", json={"is_active": False}).status_code == 400

    admin.patch(f"/api/admin/users/{lead['id']}", json={"is_active": False})
    assert login(other(), "lead.one", "Lead2026one").status_code == 401  # disabled accounts cannot sign in


@pytest.mark.parametrize("role", ["manager", "supervisor"])
def test_managers_and_supervisors_manage_scanners_only(admin, role):
    """Managers and supervisors add scanner IDs, reset a scanner's password and disable / enable it - nothing else."""
    admin.post("/api/admin/users", json={"username": f"{role}.boss", "password": "Boss2026pass", "role": role,
                                         "must_change_password": False})
    boss = other()
    assert login(boss, f"{role}.boss", "Boss2026pass").json()["user"]["role"] == role

    made = boss.post("/api/admin/users", json={"username": f"pack.{role}", "full_name": "Packer", "password": "Pack2026one"})
    assert made.status_code == 200, made.text
    packer = made.json()["user"]
    assert packer["role"] == "scanner" and packer["must_change_password"] is True
    for other_role in ("admin", "manager", "supervisor"):
        r = boss.post("/api/admin/users", json={"username": f"x.{role}.{other_role}", "password": "Nope2026xx", "role": other_role})
        assert r.status_code == 403 and "scanner" in r.json()["detail"]

    # a scanner who forgot the password / left the company
    assert boss.patch(f"/api/admin/users/{packer['id']}", json={"password": "Reset2026two"}).status_code == 200
    assert login(other(), f"pack.{role}", "Reset2026two").json()["user"]["must_change_password"] is True
    assert boss.patch(f"/api/admin/users/{packer['id']}", json={"is_active": False}).json()["user"]["is_active"] is False
    assert login(other(), f"pack.{role}", "Reset2026two").status_code == 401
    assert boss.patch(f"/api/admin/users/{packer['id']}", json={"is_active": True}).status_code == 200

    # but not the scanner's role, name or email, and never another staff account or an admin
    for change in ({"role": "supervisor"}, {"full_name": "Renamed"}, {"email": "p@example.com"}):
        assert boss.patch(f"/api/admin/users/{packer['id']}", json=change).status_code == 403
    staff = {u["username"]: u["id"] for u in admin.get("/api/admin/users").json()["users"]}
    for target in ("admin", f"{role}.boss"):
        assert boss.patch(f"/api/admin/users/{staff[target]}", json={"password": "Take2026over"}).status_code == 403
        assert boss.patch(f"/api/admin/users/{staff[target]}", json={"is_active": False}).status_code == 403

    # managers see the same pages as supervisors; settings stay admin-only
    assert boss.get("/api/admin/backups").status_code == 200
    assert boss.post("/api/admin/backups/full").status_code == 403


def test_repeated_wrong_passwords_are_slowed_down():
    c = other()
    auth_router._failures.clear()
    for _ in range(auth_router.MAX_FAILURES):
        assert login(c, "nobody@example.com", "Wrong2026x").status_code == 401
    assert login(c, "nobody@example.com", "Wrong2026x").status_code == 429
    auth_router._failures.clear()
