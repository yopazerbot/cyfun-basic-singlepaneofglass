"""Connected systems: status, manual runs, latest checks, snapshots."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import scheduler
from ..appsettings import load_config
from ..auth import require_admin, require_user
from ..config import get_settings
from ..connectors import registry
from ..connectors.base import STATUSES
from ..db import get_db
from ..models import Asset, CheckResult, ConnectorRun, User
from ..services import latest_checks, latest_runs, log_activity, snapshot_path
from ..views import redirect, render

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.get("")
def list_connectors(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    config = load_config(db)
    runs = latest_runs(db)
    tally = Counter((c.connector, c.status) for c in latest_checks(db))
    rows = []
    for key, c in registry(config).items():
        run = runs.get(key)
        counts = {s: tally[key, s] for s in STATUSES}
        rows.append(
            {
                "key": key,
                "name": c.name,
                "description": c.description,
                "configured": c.configured(),
                "run": run,
                "counts": counts,
            }
        )
    return render(request, "connectors.html", {"active": "connectors", "rows": rows, "sync_hours": config.connector_sync_hours})


@router.get("/{key}")
def connector_detail(request: Request, key: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    c = registry(load_config(db)).get(key)
    if c is None:
        return redirect("/connectors", err="Unknown connector.")
    runs = db.execute(select(ConnectorRun).where(ConnectorRun.connector == key).order_by(ConnectorRun.id.desc()).limit(20)).scalars().all()
    latest_ok = next((r for r in runs if r.status == "ok"), None)
    checks = (
        db.execute(select(CheckResult).where(CheckResult.run_id == latest_ok.id).order_by(CheckResult.status, CheckResult.check_id)).scalars().all()
        if latest_ok
        else []
    )
    assets = db.execute(select(Asset).where(Asset.source == key, Asset.lifecycle != "retired").order_by(Asset.kind, Asset.name)).scalars().all()
    return render(
        request,
        "connector_detail.html",
        {"active": "connectors", "c": c, "key": key, "runs": runs, "latest": latest_ok, "checks": checks, "assets": assets},
    )


@router.post("/{key}/run")
def run_now(request: Request, key: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    settings = get_settings()
    c = registry(load_config(db, settings)).get(key)
    if c is None or not c.configured():
        return redirect("/connectors", err="Connector is not configured. Add its credentials on the Settings page.")
    running = db.execute(select(ConnectorRun).where(ConnectorRun.connector == key, ConnectorRun.status == "running")).scalars().first()
    if running:
        return redirect(f"/connectors/{key}", err="A run is already in progress.")
    log_activity(db, user.label, "connector_trigger", "connector", key, {})
    scheduler.trigger(settings, key, user.label)
    return redirect(f"/connectors/{key}", msg="Run started. Refresh in a moment to see the results.")


@router.get("/{key}/snapshot/{run_id}")
def snapshot(request: Request, key: str, run_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    settings = get_settings()
    run = db.get(ConnectorRun, run_id)
    if run is None or run.connector != key or not run.snapshot_file:
        return redirect(f"/connectors/{key}", err="Snapshot not found.")
    path = snapshot_path(settings, run.snapshot_file)
    if path is None:
        return redirect(f"/connectors/{key}", err="Snapshot file missing.")
    return FileResponse(path, filename=f"{key}-{path.name}", media_type="application/json")
