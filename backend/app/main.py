from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text

from . import models  # noqa: F401 - register tables
from .config import ROOT_DIR, settings
from .db import Base, SessionLocal, drop_retired_indexes, engine, ensure_columns, optimize, session_scope
from .models import User
from .oms import sync as sync_module
from .routers import admin, auth, manifests, mobile_app, reconcile, reports, scan
from .security import hash_password, password_problem, websocket_user
from .services import backup
from .services.realtime import hub
from .services.scanning import clear_shipped_checks
from .timeutil import utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"


def _bootstrap_admin(*, name: str | None = None, email: str | None = None, username: str | None = None,
                     password: str | None = None) -> User | None:
    """Make sure the default admin from .env exists (ADMIN_NAME / ADMIN_EMAIL / ADMIN_USERNAME / ADMIN_PASSWORD).

    Created only when no account has that email - or, without ADMIN_EMAIL, when there are no users at all. An existing
    account is never changed, so a password changed in the app stays changed. Returns the account it created."""
    name = settings.admin_name if name is None else name
    email = (settings.admin_email if email is None else email).strip().lower()
    username = (settings.admin_username if username is None else username).strip().lower()
    password = settings.admin_password if password is None else password
    with session_scope() as db:
        any_user = (db.scalar(select(func.count(User.id))) or 0) > 0
        if email and db.scalar(select(User.id).where(User.email == email)):
            return None
        if not email and any_user:
            return None
        if not password:
            if any_user:
                log.warning("ADMIN_EMAIL %s has no account and ADMIN_PASSWORD is empty - not created", email)
                return None
            raise RuntimeError("No users exist yet: set ADMIN_PASSWORD in .env to create the first admin")
        if problem := password_problem(password, username=username, email=email):
            log.warning("ADMIN_PASSWORD is weak (%s) - the admin is still created; change it in the app", problem)
        base = username or (re.sub(r"[^a-z0-9._-]+", ".", name.strip().lower()).strip(".") or "admin")
        candidate, n = base, 1
        while db.scalar(select(User.id).where(User.username == candidate)):
            n += 1
            candidate = f"{base}{n}"
        user = User(username=candidate, full_name=name.strip() or "Administrator", email=email,
                    password_hash=hash_password(password), role="admin", password_changed_at=utcnow())
        db.add(user)
        db.flush()
        log.info("Created the default admin '%s' (%s) from .env", candidate, email or "no email")
        return user


def _fill_awb_times() -> None:
    """Orders stored before awb_generated_at existed: their AWB time is the invoice date."""
    from sqlalchemy import update

    from .models import OmsOrder

    with session_scope() as db:
        db.execute(update(OmsOrder).where(OmsOrder.awb_generated_at.is_(None), OmsOrder.tracking_norm != "")
                   .values(awb_generated_at=func.coalesce(OmsOrder.invoice_date, OmsOrder.first_seen_at)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    added = ensure_columns()
    if added:
        log.info("Database upgraded: added %s", ", ".join(added))
    if settings.scan_retention_days != settings.scan_retention_requested:
        log.warning("SCAN_RETENTION_DAYS=%s is under a year - keeping scans %s days instead "
                    "(set SCAN_RETENTION_FORCE=true if you really mean it)",
                    settings.scan_retention_requested, settings.scan_retention_days)
    if settings.scanned_orders_retention_days != settings.scanned_orders_retention_requested:
        log.warning("SCANNED_ORDERS_RETENTION_DAYS=%s is under a year - keeping scanned orders %s days instead "
                    "(set SCAN_RETENTION_FORCE=true if you really mean it)",
                    settings.scanned_orders_retention_requested, settings.scanned_orders_retention_days)
    dropped = drop_retired_indexes()
    if dropped:
        log.info("Database upgraded: replaced indexes %s", ", ".join(dropped))
    optimize(initial=True)
    _fill_awb_times()
    with session_scope() as db:
        cleared = clear_shipped_checks(db)
    if cleared:
        log.info("Cleared the old 'already shipped in OMS' check from %s scans", cleared)
    _bootstrap_admin()
    hub.bind_loop(asyncio.get_running_loop())
    if settings.sync_enabled:
        sync_module.engine_instance = sync_module.SyncEngine()
        sync_module.engine_instance.start()
        log.info("OMSGuru sync started (%s mode)", "mock" if settings.oms_use_mock else "live")
    backups = asyncio.create_task(backup.run_forever(), name="backups") if backup.enabled() else None
    if backups:
        log.info("Automatic backups to %s%s", settings.backup_dir,
                 f" (copied to {settings.backup_mirror_dir})" if settings.backup_mirror_dir else "")
    yield
    if backups:
        backups.cancel()
    if sync_module.engine_instance:
        await sync_module.engine_instance.stop()


app = FastAPI(title="Forward Scan - OMSGuru Dispatch", version="1.0.0", lifespan=lifespan)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )

for r in (auth.router, scan.router, reports.router, reconcile.router, manifests.router, admin.router, mobile_app.router):
    app.include_router(r)


STARTED_AT = __import__("time").time()


@app.get("/api/health")
def health():
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            db_ok = True
    except Exception as exc:
        log.warning("Database health check failed: %s", exc)
        raise HTTPException(status_code=503, detail="Database connectivity failure") from exc

    backend_type = "sqlite" if settings.database_url.startswith("sqlite") else "postgresql"
    eng = sync_module.engine_instance
    # Seconds since the sync scheduler last turned its loop (None = sync off). It turns every few seconds even
    # while OMSGuru is down, so a large value means the loop itself is stuck: the server watchdog restarts the app.
    loop_age = round(time.monotonic() - eng.heartbeat, 1) if eng is not None else None
    return {
        "ok": True,
        "database_backend": backend_type,
        "database_ok": db_ok,
        "mode": "mock" if settings.oms_use_mock else "live",
        "ws_clients": hub.count,
        "started_at": STARTED_AT,
        "sync_loop_age_s": loop_age,
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    def who():  # a database read: on a worker thread, never on the event loop that serves every station
        db = SessionLocal()
        try:
            return websocket_user(ws, db)
        finally:
            db.close()

    user = await asyncio.to_thread(who)
    if not user:
        await ws.close(code=4401)
        return
    await hub.connect(ws)
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text('{"event":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws)


# ---- frontend (built React app) --------------------------------------------------------------

if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    if full_path.startswith(("api/", "ws")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    candidate = (FRONTEND_DIST / full_path).resolve()
    if full_path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
        return FileResponse(candidate)
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Frontend not built. Run: cd frontend && npm install && npm run build"}, status_code=503)
