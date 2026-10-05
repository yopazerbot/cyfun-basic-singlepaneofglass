"""Verification view, snapshots and exports (official CCB workbook, audit pack, JSON)."""

from __future__ import annotations

import json
import re
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit_pack import assessment_payload, build_pack
from ..auth import require_admin, require_user
from ..config import get_settings
from ..db import get_db
from ..export_xlsx import ExportError, ExportInput, fill_workbook
from ..models import Document, Evidence, Snapshot, User
from ..scoring import summary_to_dict
from ..services import (
    by_requirement,
    checks_by_requirement,
    current_framework,
    current_summary,
    get_org,
    get_risk,
    latest_checks,
    latest_runs,
    log_activity,
    scores_by_id,
)
from ..views import redirect, render, templates

router = APIRouter(prefix="/audit", tags=["audit"])


def _verification_context(db: Session):
    fw = current_framework(db)
    return {
        "fw": fw,
        "summary": current_summary(db, fw),
        "scores": scores_by_id(db),
        "org": get_org(db),
        "risk": get_risk(db),
        "evidence": by_requirement(db.execute(select(Evidence).order_by(Evidence.id)).scalars().all()),
        "documents": by_requirement(db.execute(select(Document).where(Document.status != "retired").order_by(Document.title)).scalars().all()),
        "checks": checks_by_requirement(latest_checks(db)),
        "runs": latest_runs(db),
        "snapshots": db.execute(select(Snapshot).order_by(Snapshot.id.desc())).scalars().all(),
    }


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")
    return s[:40] or "organisation"


@router.get("")
def verification(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ctx = _verification_context(db)
    ctx["active"] = "audit"
    return render(request, "audit.html", ctx)


@router.post("/snapshot")
def create_snapshot(request: Request, name: str = Form(""), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    fw = current_framework(db)
    summary = current_summary(db, fw)
    payload = assessment_payload(db, fw)
    snap = Snapshot(
        name=name.strip()[:200] or f"Snapshot {date.today().isoformat()}",
        level=fw.level,
        taken_by=user.label,
        total_maturity=summary.total_maturity,
        passes=summary.passes,
        data=payload,
    )
    db.add(snap)
    db.commit()
    log_activity(
        db,
        user.label,
        "snapshot_create",
        "snapshot",
        str(snap.id),
        {"name": snap.name, "level": fw.level, "total": summary.total_maturity, "passes": summary.passes},
    )
    return redirect("/audit", msg=f"Snapshot '{snap.name}' stored.")


@router.get("/snapshot/{snap_id}.json")
def snapshot_json(request: Request, snap_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    snap = db.get(Snapshot, snap_id)
    if snap is None:
        return redirect("/audit", err="Snapshot not found.")
    body = json.dumps(snap.data, indent=1, ensure_ascii=False)
    return Response(
        body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="snapshot-{snap.id}-{snap.taken_at:%Y%m%d}.json"'}
    )


@router.get("/export")
def export_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = current_framework(db)
    summary = current_summary(db, fw)
    org = get_org(db)
    return render(request, "audit_export.html", {"active": "audit", "fw": fw, "summary": summary, "org": org})


@router.post("/export/workbook")
def export_workbook(request: Request, template: UploadFile = File(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = current_framework(db)
    settings = get_settings()
    if not (template.filename or "").lower().endswith(".xlsx"):
        return redirect("/audit/export", err=f"Upload the official CCB {fw.level} self-assessment workbook (.xlsx).")
    data = template.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        return redirect("/audit/export", err="File too large.")
    scores = scores_by_id(db)
    inputs = {
        rid: ExportInput(s.doc_score, s.impl_score, s.not_applicable, s.justification or "")
        for rid, s in scores.items()
        if rid in fw.by_id and (s.not_applicable or (s.doc_score is not None and s.impl_score is not None))
    }
    org = get_org(db)
    try:
        out, info = fill_workbook(data, fw, inputs, org.self_assessment_date or date.today())
    except ExportError as exc:
        return redirect("/audit/export", err=str(exc))
    log_activity(db, user.label, "export_workbook", "assessment", "", {**info, "level": fw.level})
    fname = f"{date.today().isoformat()}_CyFun2025_Self-Assessment_{fw.level}_{_slug(org.name)}.xlsx"
    return Response(
        out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


@router.get("/export/pack.zip")
def export_pack(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    settings = get_settings()
    ctx = _verification_context(db)
    fw = ctx["fw"]
    ctx["generated"] = date.today()
    html = templates.get_template("audit_pack.html").render(**ctx)
    data = build_pack(db, fw, settings, html)
    log_activity(db, user.label, "export_pack", "assessment", "", {"bytes": len(data), "level": fw.level})
    fname = f"{date.today().isoformat()}_CyFun-{fw.level}_audit-pack_{_slug(ctx['org'].name)}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/export/assessment.json")
def export_json(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = current_framework(db)
    payload = assessment_payload(db, fw)
    payload["summary"] = summary_to_dict(current_summary(db, fw))
    return Response(
        json.dumps(payload, indent=1, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{date.today().isoformat()}_cyfun-{fw.level.lower()}-assessment.json"'},
    )
