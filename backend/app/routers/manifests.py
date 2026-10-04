"""Manifests (courier hand-over batches) and the OMSGuru dispatch-update bridge."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Channel, Manifest, Scan, User
from ..security import current_user, require_supervisor
from ..services.exports import manifest_xlsx, oms_dispatch_csv
from ..services.realtime import hub
from ..timeutil import iso_utc, today_dispatch_date, utcnow
from .reports import XLSX, _file, _parse_day

router = APIRouter(prefix="/api", tags=["manifests"])

# OMSGuru's public API (v1.0.1) has no endpoint to mark an order shipped/dispatched.
# Until it does, dispatch updates go through the CSV below. Flip this when an endpoint exists
# and implement push_dispatch() in oms/client.py.
OMS_DISPATCH_API_AVAILABLE = False


def manifest_payload(m: Manifest, counts: dict[int, int]) -> dict:
    return {
        "id": m.id, "dispatch_date": m.dispatch_date.isoformat(), "channel_id": m.channel_id,
        "channel_name": m.channel.name if m.channel else "", "marketplace": m.channel.marketplace if m.channel else "",
        "color": m.channel.color if m.channel else "", "seq": m.seq, "status": m.status,
        "created_at": iso_utc(m.created_at), "closed_at": iso_utc(m.closed_at), "count": counts.get(m.id, 0),
        "number": f"M-{m.dispatch_date.strftime('%Y%m%d')}-{m.channel_id}-{m.seq}",
    }


@router.get("/manifests")
def list_manifests(day: str | None = Query(None, alias="date"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    d = _parse_day(day)
    ms = list(db.scalars(select(Manifest).where(Manifest.dispatch_date == d).order_by(Manifest.channel_id, Manifest.seq)).unique())
    counts = dict(db.execute(
        select(Scan.manifest_id, func.count(Scan.id)).where(Scan.manifest_id.in_([m.id for m in ms] or [0])).group_by(Scan.manifest_id)
    ).all())
    return {"date": d.isoformat(), "manifests": [manifest_payload(m, counts) for m in ms]}


@router.post("/manifests/{manifest_id}/close")
def close_manifest(manifest_id: int, db: Session = Depends(get_db), user: User = Depends(require_supervisor)):
    m = db.get(Manifest, manifest_id)
    if not m:
        raise HTTPException(404, "Manifest not found")
    if m.status == "CLOSED":
        return {"ok": True}
    m.status = "CLOSED"
    m.closed_at = utcnow()
    m.closed_by_id = user.id
    db.commit()
    hub.publish("manifest", {"id": m.id, "status": m.status})
    return {"ok": True}


@router.post("/manifests/{manifest_id}/reopen")
def reopen_manifest(manifest_id: int, db: Session = Depends(get_db), user: User = Depends(require_supervisor)):
    m = db.get(Manifest, manifest_id)
    if not m:
        raise HTTPException(404, "Manifest not found")
    other_open = db.scalar(select(Manifest.id).where(
        Manifest.dispatch_date == m.dispatch_date, Manifest.channel_id == m.channel_id, Manifest.status == "OPEN"))
    if other_open:
        raise HTTPException(400, "Another manifest for this channel and day is already open - close it first")
    m.status, m.closed_at = "OPEN", None
    db.commit()
    hub.publish("manifest", {"id": m.id, "status": m.status})
    return {"ok": True}


@router.get("/manifests/{manifest_id}/export.xlsx")
def export_manifest(manifest_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = db.get(Manifest, manifest_id)
    if not m:
        raise HTTPException(404, "Manifest not found")
    scans = list(db.scalars(select(Scan).where(Scan.manifest_id == m.id).order_by(Scan.scanned_at)).unique())
    number = f"M-{m.dispatch_date.strftime('%Y%m%d')}-{m.channel_id}-{m.seq}"
    content = manifest_xlsx(scans, channel_name=m.channel.name if m.channel else "", dispatch_date=m.dispatch_date.strftime("%d-%m-%Y"),
                            manifest_no=number, closed_at=m.closed_at)
    return _file(content, f"{number}.xlsx", XLSX)


# ---- OMSGuru dispatch update ---------------------------------------------------------------


@router.get("/oms-dispatch/summary")
def oms_dispatch_summary(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(
        select(Scan.dispatch_date, Scan.channel_id, Scan.oms_update_status, func.count(Scan.id))
        .group_by(Scan.dispatch_date, Scan.channel_id, Scan.oms_update_status)
        .order_by(Scan.dispatch_date.desc())
    ).all()
    chans = {c.id: c for c in db.scalars(select(Channel))}
    agg: dict[tuple, dict] = {}
    for d, cid, st, n in rows:
        key = (d, cid)
        a = agg.setdefault(key, {"dispatch_date": d.isoformat(), "channel_id": cid,
                                 "channel_name": chans[cid].name if cid in chans else "",
                                 "PENDING": 0, "EXPORTED": 0, "DONE": 0, "FAILED": 0})
        a[st] = a.get(st, 0) + n
    items = [a for a in agg.values() if a["PENDING"] or a["EXPORTED"] or a["FAILED"]][:200]
    return {"api_available": OMS_DISPATCH_API_AVAILABLE, "items": items}


@router.get("/oms-dispatch/export.csv")
def oms_dispatch_export(
    day: str | None = Query(None, alias="date"), channel_id: int | None = None, include_exported: bool = False,
    mark: bool = True, db: Session = Depends(get_db), user: User = Depends(require_supervisor),
):
    d = _parse_day(day)
    statuses = ["PENDING", "FAILED"] + (["EXPORTED"] if include_exported else [])
    stmt = select(Scan).where(Scan.dispatch_date == d, Scan.oms_update_status.in_(statuses), Scan.result != "UNVERIFIED")
    if channel_id:
        stmt = stmt.where(Scan.channel_id == channel_id)
    scans = list(db.scalars(stmt.order_by(Scan.channel_id, Scan.scanned_at)).unique())
    content = oms_dispatch_csv(scans)
    if mark and scans:
        now = utcnow()
        for s in scans:
            if s.oms_update_status != "DONE":
                s.oms_update_status, s.oms_update_at = "EXPORTED", now
                s.oms_update_note = f"Exported by {user.username}"
        db.commit()
    return _file("﻿" + content, f"oms_dispatch_{d.isoformat()}{'_' + str(channel_id) if channel_id else ''}.csv",
                 "text/csv; charset=utf-8")


class MarkDoneIn(BaseModel):
    date: date
    channel_id: int | None = None


@router.post("/oms-dispatch/mark-done")
def oms_dispatch_mark_done(body: MarkDoneIn, db: Session = Depends(get_db), user: User = Depends(require_supervisor)):
    """Confirm the exported file was uploaded into OMSGuru."""
    stmt = update(Scan).where(Scan.dispatch_date == body.date, Scan.oms_update_status == "EXPORTED")
    if body.channel_id:
        stmt = stmt.where(Scan.channel_id == body.channel_id)
    res = db.execute(stmt.values(oms_update_status="DONE", oms_update_at=utcnow(),
                                 oms_update_note=f"Confirmed in OMS by {user.username}"))
    db.commit()
    return {"updated": res.rowcount or 0, "today": today_dispatch_date().isoformat()}
