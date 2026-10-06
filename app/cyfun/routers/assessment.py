"""Self-assessment: overview and per-requirement detail with scores, evidence, checks and actions."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import service as ai_service
from ..auth import require_admin, require_user
from ..config import get_settings
from ..db import get_db
from ..models import Action, Activity, Document, Evidence, Score, User
from ..scoring import ReqInput, na_blocked_reason, validate_input
from ..services import (
    by_requirement,
    checks_by_requirement,
    current_framework,
    current_summary,
    latest_checks,
    log_activity,
    parse_date,
    parse_int,
    scores_by_id,
)
from ..views import redirect, render
from .actions import PRIORITIES
from .evidence import attach_file_or_link, log_evidence_added

router = APIRouter(prefix="/assessment", tags=["assessment"])
LEVEL_FILTERS = ("all", "Basic", "Important", "Essential", "km", "unscored", "below")


@router.get("")
def overview(request: Request, level: str = "all", user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = current_framework(db)
    summary = current_summary(db, fw)
    scores = scores_by_id(db)
    evidence = by_requirement(db.execute(select(Evidence)).scalars().all())
    documents = by_requirement(db.execute(select(Document).where(Document.status != "retired")).scalars().all())
    checks = checks_by_requirement(latest_checks(db))
    actions = by_requirement(db.execute(select(Action).where(Action.status.in_(["open", "in_progress"]))).scalars().all(), "requirement_id")
    flt = level if level in LEVEL_FILTERS else "all"

    def visible(r) -> bool:
        res = summary.requirements[r.id]
        if flt in ("Basic", "Important", "Essential"):
            return r.level == flt
        if flt == "km":
            return r.key_measure
        if flt == "unscored":
            return not res.scored
        if flt == "below":
            return res.maturity is not None and res.maturity < summary.target
        return True

    shown = {r.id for r in fw.requirements if visible(r)}
    return render(
        request,
        "assessment_list.html",
        {
            "active": "assessment",
            "fw": fw,
            "summary": summary,
            "scores": scores,
            "evidence": evidence,
            "documents": documents,
            "checks": checks,
            "actions": actions,
            "flt": flt,
            "shown": shown,
            "counts": fw.count_by_level(),
        },
    )


def _next_unscored(fw, summary, rid: str):
    """The first unscored requirement after `rid` in framework order, wrapping around; None when all are scored."""
    reqs = fw.requirements
    start = next((i + 1 for i, r in enumerate(reqs) if r.id == rid), 0)
    for r in reqs[start:] + reqs[:start]:
        if r.id != rid and not summary.requirements[r.id].scored:
            return r
    return None


@router.get("/{rid}")
def detail(request: Request, rid: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = current_framework(db)
    req = fw.get(rid)
    if req is None:
        return redirect("/assessment", err=f"{rid} is not part of the {fw.level} requirement set.")
    summary = current_summary(db, fw)
    score = db.get(Score, rid)
    evidence = [e for e in db.execute(select(Evidence).order_by(Evidence.id.desc())).scalars().all() if rid in (e.requirement_ids or [])]
    documents = [d for d in db.execute(select(Document).order_by(Document.title)).scalars().all() if rid in (d.requirement_ids or [])]
    checks = [c for c in latest_checks(db) if rid in (c.requirement_ids or [])]
    actions = db.execute(select(Action).where(Action.requirement_id == rid).order_by(Action.status, Action.due_date)).scalars().all()
    hq = select(Activity).where(Activity.entity == "score", Activity.entity_id == rid)
    if user.role != "admin":
        hq = hq.where(Activity.action.not_like("ai!_%", escape="!"))  # Claude review events are internal working data
    history = db.execute(hq.order_by(Activity.id.desc()).limit(10)).scalars().all()
    prev_, next_ = fw.neighbours(rid)
    next_unscored = _next_unscored(fw, summary, rid)
    blocked = na_blocked_reason(req, fw.thresholds)
    na_available = blocked is None and (summary.na_count < summary.na_allowed or (score is not None and score.not_applicable))
    claude = {}
    if user.role == "admin":
        ai_service.recover_stale(db)
        claude = {"prop": ai_service.latest_for(db, rid), "ai": ai_service.status(db, get_settings()), "rid": rid}
    return render(
        request,
        "assessment_detail.html",
        {
            "active": "assessment",
            "fw": fw,
            "req": req,
            "res": summary.requirements[rid],
            "summary": summary,
            "score": score,
            "evidence": evidence,
            "documents": documents,
            "checks": checks,
            "actions": actions,
            "history": history,
            "prev": prev_,
            "next": next_,
            "next_unscored": next_unscored,
            "na_available": na_available,
            "na_blocked": blocked,
            **claude,
        },
    )


@router.post("/{rid}/score")
def save_score(
    request: Request,
    rid: str,
    doc_score: str = Form(""),
    impl_score: str = Form(""),
    not_applicable: str = Form(""),
    justification: str = Form(""),
    go: str = Form("stay"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    fw = current_framework(db)
    req = fw.get(rid)
    if req is None:
        return redirect("/assessment", err=f"{rid} is not part of the {fw.level} requirement set.")
    na = not_applicable == "1"
    errors: list[str] = []
    parsed: dict[str, int | None] = {}
    for label, raw in (("documentation", doc_score), ("implementation", impl_score)):
        value = parse_int(raw, 1, 5)
        if raw.strip() and value is None:
            errors.append(f"The {label} score must be a whole number from 1 to 5.")
        parsed[label] = value
    inp = ReqInput(parsed["documentation"], parsed["implementation"], na)
    errors += validate_input(req, inp, fw.thresholds)
    if na:
        others = [
            s
            for s in db.execute(select(Score).where(Score.not_applicable.is_(True))).scalars().all()
            if s.requirement_id != rid and s.requirement_id in fw.by_id
        ]
        allowed = int(fw.thresholds["na_allowed"])
        if len(others) >= allowed:
            errors.append(f"{fw.level} allows at most {allowed} not applicable requirement(s); already used by {', '.join(o.requirement_id for o in others)}.")
        if not justification.strip():
            errors.append("A justification is required when marking a requirement not applicable.")
    if errors:
        return redirect(f"/assessment/{rid}", err=" ".join(errors))
    score = db.get(Score, rid)
    if score is None:
        score = Score(requirement_id=rid)
        db.add(score)
    before = {"doc": score.doc_score, "impl": score.impl_score, "na": score.not_applicable}
    score.doc_score = None if na else inp.doc
    score.impl_score = None if na else inp.impl
    score.not_applicable = na
    score.justification = justification.strip()
    score.updated_by = user.label
    score.ai_proposal_id = None  # a manual save makes the scores a manual decision again
    db.commit()
    after = {"doc": score.doc_score, "impl": score.impl_score, "na": score.not_applicable}
    if before != after:
        log_activity(db, user.label, "score_update", "score", rid, {"before": before, "after": after, "level": fw.level})
    else:
        log_activity(db, user.label, "justification_update", "score", rid, {})
    if go == "next":
        _, nxt = fw.neighbours(rid)
        if nxt:
            return redirect(f"/assessment/{nxt.id}", msg=f"{rid} saved.")
    elif go == "next_unscored":
        nxt = _next_unscored(fw, current_summary(db, fw), rid)
        if nxt:
            return redirect(f"/assessment/{nxt.id}", msg=f"{rid} saved.")
        return redirect("/assessment", msg=f"{rid} saved. Every requirement of {fw.level} is now scored.")
    return redirect(f"/assessment/{rid}", msg="Saved.")


@router.post("/{rid}/evidence")
def add_evidence(
    request: Request,
    rid: str,
    title: str = Form(""),
    description: str = Form(""),
    url: str = Form(""),
    share_with_ai: str = Form(""),
    file: UploadFile | None = File(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    fw = current_framework(db)
    if fw.get(rid) is None:
        return redirect("/assessment", err="Unknown requirement.")
    ev = Evidence(title=title.strip()[:300], description=description.strip(), requirement_ids=[rid], collected_on=date.today(), collected_by=user.label)
    error = attach_file_or_link(ev, file, url, share_with_ai == "1")
    if error:
        return redirect(f"/assessment/{rid}", err=error)
    db.add(ev)
    db.commit()
    log_evidence_added(db, user, ev)
    return redirect(f"/assessment/{rid}", msg="Evidence added.")


@router.post("/{rid}/action")
def add_action(
    request: Request,
    rid: str,
    title: str = Form(...),
    owner: str = Form(""),
    due_date: str = Form(""),
    priority: str = Form("medium"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    fw = current_framework(db)
    if fw.get(rid) is None:
        return redirect("/assessment", err="Unknown requirement.")
    a = Action(
        title=title.strip()[:300] or "Untitled action",
        requirement_id=rid,
        owner=owner.strip()[:200],
        due_date=parse_date(due_date),
        priority=priority if priority in PRIORITIES else "medium",
    )
    db.add(a)
    db.commit()
    log_activity(db, user.label, "action_create", "action", str(a.id), {"title": a.title, "requirement": rid})
    return redirect(f"/assessment/{rid}", msg="Action added.")
