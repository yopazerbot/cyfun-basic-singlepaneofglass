"""Dashboard: the single pane."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..appsettings import load_config
from ..auth import require_user
from ..connectors import registry
from ..connectors.base import STATUSES
from ..db import get_db
from ..models import Activity, Document, Evidence, User
from ..services import JOURNEY_STAGES, current_framework, current_summary, documents_due, get_org, get_risk, latest_checks, latest_runs, open_actions
from ..views import render

router = APIRouter(tags=["dashboard"])


@router.get("/")
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = current_framework(db)
    summary = current_summary(db, fw)
    org = get_org(db)
    risk = get_risk(db)

    evidence = db.execute(select(Evidence)).scalars().all()
    documents = db.execute(select(Document).where(Document.status == "approved")).scalars().all()
    covered: set[str] = set()
    for e in evidence:
        covered.update(e.requirement_ids or [])
    for d in documents:
        covered.update(d.requirement_ids or [])
    covered &= set(fw.by_id)

    checks = latest_checks(db)
    check_counts = {s: sum(1 for c in checks if c.status == s) for s in STATUSES}
    runs = latest_runs(db)
    conns = [{"key": key, "name": c.name, "configured": c.configured(), "run": runs.get(key)} for key, c in registry(load_config(db)).items()]

    actions = open_actions(db)
    today = date.today()
    overdue = [a for a in actions if a.due_date and a.due_date < today]
    journey = org.journey or {}
    stages = [{"key": k, "label": lbl, **(journey.get(k) or {"status": "not_started"})} for k, lbl in JOURNEY_STAGES]
    done_stages = sum(1 for s in stages if s.get("status") == "done")
    recent = db.execute(select(Activity).order_by(Activity.id.desc()).limit(8)).scalars().all()

    return render(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "fw": fw,
            "summary": summary,
            "org": org,
            "risk": risk,
            "level_mismatch": bool(risk and risk.level and risk.level != fw.level),
            "covered": covered,
            "coverage_pct": round(100 * len(covered) / len(fw.requirements)) if fw.requirements else 0,
            "checks": checks,
            "check_counts": check_counts,
            "connectors": conns,
            "actions": actions,
            "overdue": overdue,
            "docs_due": documents_due(db),
            "stages": stages,
            "done_stages": done_stages,
            "recent": recent,
        },
    )
