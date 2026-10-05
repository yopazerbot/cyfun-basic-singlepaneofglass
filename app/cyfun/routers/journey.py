"""Scope, organisation details and the label journey stages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..auth import require_admin, require_user
from ..db import get_db
from ..models import User
from ..services import JOURNEY_STAGES, get_org, log_activity, parse_date
from ..views import redirect, render

router = APIRouter(prefix="/journey", tags=["journey"])
STAGE_STATUS = ("not_started", "in_progress", "done")


@router.get("")
def journey(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    org = get_org(db)
    j = org.journey or {}
    stages = [{"key": k, "label": lbl, **(j.get(k) or {"status": "not_started", "date": "", "note": ""})} for k, lbl in JOURNEY_STAGES]
    return render(request, "journey.html", {"active": "journey", "org": org, "stages": stages, "statuses": STAGE_STATUS})


@router.post("/organisation")
def save_org(
    request: Request,
    name: str = Form(""),
    legal_entity: str = Form(""),
    enterprise_number: str = Form(""),
    contact: str = Form(""),
    scope_description: str = Form(""),
    scope_exclusions: str = Form(""),
    cab_name: str = Form(""),
    self_assessment_date: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    org = get_org(db)
    org.name = name.strip()[:200]
    org.legal_entity = legal_entity.strip()[:200]
    org.enterprise_number = enterprise_number.strip()[:50]
    org.contact = contact.strip()[:200]
    org.scope_description = scope_description.strip()
    org.scope_exclusions = scope_exclusions.strip()
    org.cab_name = cab_name.strip()[:200]
    org.self_assessment_date = parse_date(self_assessment_date)
    db.commit()
    log_activity(db, user.email, "organisation_update", "organisation", "1", {"name": org.name})
    return redirect("/journey", msg="Organisation and scope saved.")


@router.post("/stage/{stage}")
def save_stage(
    request: Request,
    stage: str,
    status: str = Form("not_started"),
    date: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    keys = {k for k, _ in JOURNEY_STAGES}
    if stage not in keys or status not in STAGE_STATUS:
        return redirect("/journey", err="Unknown stage or status.")
    org = get_org(db)
    j = dict(org.journey or {})
    d = parse_date(date)
    j[stage] = {"status": status, "date": d.isoformat() if d else "", "note": note.strip()[:1000]}
    org.journey = j
    db.commit()
    log_activity(db, user.email, "journey_stage", "journey", stage, j[stage])
    return redirect("/journey", msg=f"Stage updated: {dict(JOURNEY_STAGES)[stage]}.")
