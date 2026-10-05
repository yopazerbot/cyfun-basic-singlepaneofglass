"""Asset inventory: hardware, software, services, data, network, cloud, identities."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_user
from ..db import get_db
from ..models import Asset, User
from ..services import log_activity
from ..views import redirect, render

router = APIRouter(prefix="/assets", tags=["assets"])
KINDS = ("hardware", "software", "service", "data", "network", "cloud", "identity")
CLASSIFICATIONS = ("Public", "Internal", "Confidential", "Restricted")
CRITICALITY = ("Low", "Medium", "High")
LIFECYCLE = ("planned", "active", "retired")


def _query(db: Session, kind: str, source: str, lifecycle: str, q: str):
    stmt = select(Asset)
    if kind in KINDS:
        stmt = stmt.where(Asset.kind == kind)
    if source:
        stmt = stmt.where(Asset.source == source)
    if lifecycle in LIFECYCLE:
        stmt = stmt.where(Asset.lifecycle == lifecycle)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Asset.name.ilike(like) | Asset.description.ilike(like) | Asset.owner.ilike(like))
    return db.execute(stmt.order_by(Asset.kind, Asset.name)).scalars().all()


@router.get("")
def list_assets(
    request: Request,
    kind: str = "",
    source: str = "",
    lifecycle: str = "active",
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    items = _query(db, kind, source, lifecycle, q)
    sources = sorted(set(db.execute(select(Asset.source).distinct()).scalars().all()))
    counts = {k: 0 for k in KINDS}
    for a in db.execute(select(Asset).where(Asset.lifecycle != "retired")).scalars().all():
        counts[a.kind] = counts.get(a.kind, 0) + 1
    return render(
        request,
        "assets.html",
        {
            "active": "assets",
            "items": items,
            "kinds": KINDS,
            "sources": sources,
            "counts": counts,
            "f_kind": kind,
            "f_source": source,
            "f_lifecycle": lifecycle,
            "q": q,
            "classifications": CLASSIFICATIONS,
            "criticality": CRITICALITY,
            "lifecycles": LIFECYCLE,
            "edit": None,
        },
    )


@router.get("/export.csv")
def export_csv(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    items = db.execute(select(Asset).order_by(Asset.kind, Asset.name)).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["kind", "name", "description", "owner", "location", "classification", "criticality", "primary", "lifecycle", "source", "external_id", "last_seen"]
    )
    for a in items:
        w.writerow(
            [
                a.kind,
                a.name,
                a.description,
                a.owner,
                a.location,
                a.classification,
                a.criticality,
                "yes" if a.primary_asset else "no",
                a.lifecycle,
                a.source,
                a.external_id,
                a.last_seen_at.isoformat() if a.last_seen_at else "",
            ]
        )
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="assets.csv"'})


@router.get("/{asset_id}")
def edit_asset(request: Request, asset_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    item = db.get(Asset, asset_id)
    if item is None:
        return redirect("/assets", err="Asset not found.")
    return render(
        request,
        "asset_form.html",
        {"active": "assets", "edit": item, "kinds": KINDS, "classifications": CLASSIFICATIONS, "criticality": CRITICALITY, "lifecycles": LIFECYCLE},
    )


def _apply(item: Asset, form, manual: bool) -> None:
    if manual:
        item.kind = form.get("kind") if form.get("kind") in KINDS else item.kind
        item.name = (form.get("name") or "").strip()[:300] or item.name
        item.description = (form.get("description") or "").strip()[:2000]
        item.location = (form.get("location") or "").strip()[:200]
        item.lifecycle = form.get("lifecycle") if form.get("lifecycle") in LIFECYCLE else item.lifecycle
    item.owner = (form.get("owner") or "").strip()[:200]
    item.classification = form.get("classification") if form.get("classification") in CLASSIFICATIONS else item.classification
    item.criticality = form.get("criticality") if form.get("criticality") in CRITICALITY else item.criticality
    item.primary_asset = form.get("primary_asset") == "1"


@router.post("")
async def create_asset(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return redirect("/assets", err="Name is required.")
    item = Asset(kind=form.get("kind") if form.get("kind") in KINDS else "hardware", name=name[:300], source="manual")
    _apply(item, form, manual=True)
    db.add(item)
    db.commit()
    log_activity(db, user.email, "asset_create", "asset", str(item.id), {"name": item.name, "kind": item.kind})
    return redirect("/assets", msg="Asset added.")


@router.post("/{asset_id}")
async def update_asset(request: Request, asset_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(Asset, asset_id)
    if item is None:
        return redirect("/assets", err="Asset not found.")
    form = await request.form()
    _apply(item, form, manual=item.source == "manual")
    db.commit()
    log_activity(db, user.email, "asset_update", "asset", str(item.id), {"name": item.name})
    return redirect("/assets", msg="Asset updated.")


@router.post("/{asset_id}/delete")
def delete_asset(request: Request, asset_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(Asset, asset_id)
    if item is None:
        return redirect("/assets", err="Asset not found.")
    if item.source != "manual":
        return redirect("/assets", err="Connector-synced assets cannot be deleted; they are retired automatically when they disappear from the source.")
    db.delete(item)
    db.commit()
    log_activity(db, user.email, "asset_delete", "asset", str(asset_id), {"name": item.name})
    return redirect("/assets", msg="Asset deleted.")
