"""Public Android app distribution: version check, APK download, download page and QR code (no sign-in needed)."""
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

APK_BYTES = b"PK\x03\x04fake-apk-for-tests" * 64


def _publish(version_code=10021, version_name="1.0.21", notes="Pending list on the scan screen", min_code=0):
    d = Path(settings.app_release_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "forward-scan-app.apk").write_bytes(APK_BYTES)
    (d / "latest.json").write_text(json.dumps({
        "version_code": version_code, "version_name": version_name, "notes": notes, "size": len(APK_BYTES),
        "sha256": "abc", "published_at": "2026-10-07T10:00:00Z", "min_version_code": min_code, "commit": "deadbeef",
    }), encoding="utf-8")


def _unpublish():
    shutil.rmtree(settings.app_release_dir, ignore_errors=True)


def test_nothing_published_yet():
    _unpublish()
    with TestClient(app) as c:
        r = c.get("/api/app/latest")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is False
        assert body["page_url"].endswith("/download")
        assert c.get("/download/forward-scan.apk").status_code == 404
        page = c.get("/download")
        assert page.status_code == 200
        assert "not been published" in page.text


def test_latest_release_is_public_and_downloadable():
    _publish()
    try:
        with TestClient(app) as c:
            c.cookies.clear()
            r = c.get("/api/app/latest")  # no sign-in: the app checks before login, packers open the link
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["available"] is True
            assert body["version_code"] == 10021
            assert body["version_name"] == "1.0.21"
            assert body["size"] == len(APK_BYTES)
            assert body["min_version_code"] == 0
            assert body["download_url"].endswith("/download/forward-scan.apk")

            apk = c.get("/download/forward-scan.apk")
            assert apk.status_code == 200
            assert apk.headers["content-type"] == "application/vnd.android.package-archive"
            assert "ForwardScan-1.0.21.apk" in apk.headers["content-disposition"]
            assert apk.content == APK_BYTES

            page = c.get("/download")
            assert page.status_code == 200
            assert "1.0.21" in page.text and "Pending list on the scan screen" in page.text
            assert "<svg" in page.text  # QR code for computer screens

            qr = c.get("/download/qr.svg")
            assert qr.status_code == 200
            assert qr.headers["content-type"].startswith("image/svg+xml")
            assert qr.text.lstrip().startswith("<svg")
    finally:
        _unpublish()


def test_links_follow_the_proxy_host():
    _publish()
    try:
        with TestClient(app) as c:
            r = c.get("/api/app/latest", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "scan.example.com"})
            body = r.json()
            assert body["page_url"] == "https://scan.example.com/download"
            assert body["download_url"] == "https://scan.example.com/download/forward-scan.apk"
    finally:
        _unpublish()


def test_release_notes_are_escaped_on_the_page():
    _publish(notes="<script>alert(1)</script>")
    try:
        with TestClient(app) as c:
            page = c.get("/download")
            assert "<script>alert(1)</script>" not in page.text
            assert "&lt;script&gt;" in page.text
    finally:
        _unpublish()


def test_broken_metadata_is_treated_as_not_published():
    _publish()
    (Path(settings.app_release_dir) / "latest.json").write_text("{not json", encoding="utf-8")
    try:
        with TestClient(app) as c:
            assert c.get("/api/app/latest").json()["available"] is False
            assert c.get("/download/forward-scan.apk").status_code == 404
    finally:
        _unpublish()
