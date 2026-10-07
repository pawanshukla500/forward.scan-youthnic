"""Android app distribution: a public download page + QR code, and the version check the app uses to offer updates.

The APK build workflow (.github/workflows/mobile-apk.yml) copies two files into APP_RELEASE_DIR (data/app on the
server, kept across deploys):
    forward-scan-app.apk   the signed release APK
    latest.json            {"version_code", "version_name", "notes", "size", "sha256", "published_at",
                            "min_version_code", "commit"}
Nothing here needs a sign-in: the link / QR is shared with packers before they have the app, and the app checks
for updates before (and independently of) login. The APK itself holds no secrets.
"""
from __future__ import annotations

import html
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..config import settings

log = logging.getLogger("mobile_app")

router = APIRouter(tags=["mobile-app"])

APK_NAME = "forward-scan-app.apk"
APK_MEDIA_TYPE = "application/vnd.android.package-archive"
PAGE_PATH = "/download"
APK_PATH = "/download/forward-scan.apk"
QR_PATH = "/download/qr.svg"


def _dir() -> Path:
    return Path(settings.app_release_dir)


def release_info() -> dict | None:
    """The published release, or None when no APK has been published to this server yet."""
    apk = _dir() / APK_NAME
    meta = _dir() / "latest.json"
    if not apk.is_file() or not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        code = int(data.get("version_code") or 0)
    except (ValueError, OSError, TypeError) as exc:
        log.warning("Unreadable %s: %s", meta, exc)
        return None
    if code <= 0:
        return None
    return {
        "version_code": code,
        "version_name": str(data.get("version_name") or code),
        "notes": str(data.get("notes") or "")[:2000],
        "size": apk.stat().st_size,
        "sha256": str(data.get("sha256") or ""),
        "published_at": str(data.get("published_at") or ""),
        "min_version_code": int(data.get("min_version_code") or 0),
        "commit": str(data.get("commit") or "")[:40],
    }


def base_url(request: Request) -> str:
    """https://host of this server as phones reach it (PUBLIC_URL, else the proxy's forwarded headers)."""
    if settings.public_url:
        return settings.public_url
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


def _qr_svg(url: str, scale: int = 6) -> str:
    import segno

    buf = io.BytesIO()
    segno.make(url, error="m", micro=False).save(
        buf, kind="svg", scale=scale, border=2, dark="#0F172A", light="#FFFFFF", xmldecl=False, svgns=True,
    )
    return buf.getvalue().decode("utf-8")


@router.get("/api/app/latest")
def latest(request: Request):
    """Version check for the installed app, and the details the Admin page shows next to the QR code."""
    base = base_url(request)
    info = release_info()
    links = {"page_url": base + PAGE_PATH, "qr_url": base + QR_PATH}
    if not info:
        return {"available": False, **links}
    return {"available": True, **info, "download_url": base + APK_PATH, **links}


@router.get(APK_PATH, include_in_schema=False)
def download_apk():
    info = release_info()
    if not info:
        raise HTTPException(404, "The Android app has not been published to this server yet")
    return FileResponse(
        _dir() / APK_NAME,
        media_type=APK_MEDIA_TYPE,
        filename=f"ForwardScan-{info['version_name']}.apk",
        headers={"Cache-Control": "no-cache"},
    )


@router.get(QR_PATH, include_in_schema=False)
def download_qr(request: Request):
    return Response(_qr_svg(base_url(request) + PAGE_PATH, scale=8), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})


def _fmt_size(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return ""


_SCAN_MARK = (
    '<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true"><path d="M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 '
    '2h-3M8 21H5a2 2 0 0 1-2-2v-3"/><path d="M7 12h10M8 9v6M11 9v6M14 9v6M17 9v6"/></svg>'
)

_PAGE_CSS = """
:root{--bg:#F8FAFC;--card:#FFFFFF;--ink:#0F172A;--muted:#475569;--line:#E2E8F0;--brand:#126B4E;--brand-ink:#0D553D;--soft:#ECFDF5}
@media (prefers-color-scheme: dark){:root{--bg:#0B1220;--card:#111A2B;--ink:#E2E8F0;--muted:#94A3B8;--line:#1E293B;--brand:#22A06B;--brand-ink:#4ADE80;--soft:#0F2A20}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:560px;margin:0 auto;padding:24px 16px 40px}
.head{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.logo{width:48px;height:48px;border-radius:12px;background:#126B4E;display:grid;place-items:center;flex:none}
h1{font-size:22px;margin:0;line-height:1.2}
.sub{color:var(--muted);font-size:14px;margin:2px 0 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
.meta{display:flex;flex-wrap:wrap;gap:8px 16px;color:var(--muted);font-size:14px;margin-bottom:14px}
.meta b{color:var(--ink)}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;min-height:52px;border-radius:12px;background:var(--brand);color:#fff;
  font-weight:700;font-size:17px;text-decoration:none}
.btn:active{transform:translateY(1px)}
h2{font-size:15px;margin:0 0 10px}
ol{margin:0;padding-left:20px;color:var(--muted);font-size:14px}
ol li{margin:6px 0}
.notes{white-space:pre-line;color:var(--muted);font-size:14px;margin:0}
.qr{display:flex;gap:16px;align-items:center}
.qr svg{width:148px;height:148px;flex:none;border-radius:10px;border:1px solid var(--line)}
.qr p{margin:0;color:var(--muted);font-size:14px}
.empty{color:var(--muted)}
.foot{color:var(--muted);font-size:12px;text-align:center;margin-top:18px}
"""


@router.get(PAGE_PATH, include_in_schema=False, response_class=HTMLResponse)
def download_page(request: Request):
    base = base_url(request)
    info = release_info()
    if info:
        when = _fmt_date(info["published_at"])
        notes = html.escape(info["notes"]).strip()
        body = f"""
<div class="card">
  <div class="meta"><span>Version <b>{html.escape(info['version_name'])}</b></span><span>{_fmt_size(info['size'])}</span>
  {f'<span>Updated {when}</span>' if when else ''}</div>
  <a class="btn" href="{APK_PATH}" download>Download the app (APK)</a>
</div>
{f'<div class="card"><h2>What is new</h2><p class="notes">{notes}</p></div>' if notes else ''}
<div class="card">
  <h2>How to install</h2>
  <ol>
    <li>Tap <b>Download the app</b> and open the file when the download finishes.</li>
    <li>If Android asks, allow <b>Install unknown apps</b> for your browser, then tap <b>Install</b>.</li>
    <li>Already have Forward Scan? Install over it - you stay signed in. Later updates show up inside the app.</li>
    <li>If it says <b>App not installed</b>, uninstall the old Forward Scan once and install again.</li>
  </ol>
</div>"""
    else:
        body = '<div class="card"><p class="empty">The Android app has not been published to this server yet. Ask your admin.</p></div>'
    qr = _qr_svg(base + PAGE_PATH, scale=6)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forward Scan app</title><meta name="robots" content="noindex"><meta name="color-scheme" content="light dark">
<style>{_PAGE_CSS}</style></head>
<body><main>
<div class="head"><div class="logo">{_SCAN_MARK}</div>
<div><h1>Forward Scan for Android</h1><p class="sub">Dispatch scanning app for warehouse phones</p></div></div>
{body}
<div class="card qr">{qr}<p>On a computer? Scan this code with the phone camera to open this page on the phone.</p></div>
<p class="foot">{html.escape(base + PAGE_PATH)}</p>
</main></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})
