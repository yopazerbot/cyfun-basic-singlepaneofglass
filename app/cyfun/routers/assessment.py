"""Self-assessment: overview and per-requirement detail with scores, evidence, checks and actions."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_user
from ..config import get_settings
from ..db import get_db
from ..framework import load_framework
from ..models import Action, Activity, Document, Evidence, Score, User
from ..scoring import ReqInput, validate_input
from ..services import by_requirement, checks_by_requirement, current_summary, latest_checks, log_activity, parse_int, scores_by_id, store_upload
from ..views import redirect, render

router = APIRouter(prefix="/assessment", tags=["assessment"])


@router.get("")
def overview(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = load_framework()
    summary = current_summary(db, fw)
    scores = scores_by_id(db)
    evidence = by_requirement(db.execute(select(Evidence)).scalars().all())
    documents = by_requirement(db.execute(select(Document).where(Document.status != "retired")).scalars().all())
    checks = checks_by_requirement(latest_checks(db))
    actions = by_requirement(db.execute(select(Action).where(Action.status.in_(["open", "in_progress"])).select_from(Action)).scalars().all(), "requirement_id")
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
        },
    )


@router.get("/{rid}")
def detail(request: Request, rid: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = load_framework()
    req = fw.get(rid)
    if req is None:
        return redirect("/assessment", err="Unknown requirement.")
    summary = current_summary(db, fw)
    score = db.get(Score, rid)
    evidence = [e for e in db.execute(select(Evidence).order_by(Evidence.id.desc())).scalars().all() if rid in (e.requirement_ids or [])]
    documents = [d for d in db.execute(select(Document).order_by(Document.title)).scalars().all() if rid in (d.requirement_ids or [])]
    checks = [c for c in latest_checks(db) if rid in (c.requirement_ids or [])]
    actions = db.execute(select(Action).where(Action.requirement_id == rid).order_by(Action.status, Action.due_date)).scalars().all()
    history = db.execute(select(Activity).where(Activity.entity == "score", Activity.entity_id == rid).order_by(Activity.id.desc()).limit(10)).scalars().all()
    prev_, next_ = fw.neighbours(rid)
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
            "na_available": summary.na_count == 0 or (score is not None and score.not_applicable),
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
    fw = load_framework()
    req = fw.get(rid)
    if req is None:
        return redirect("/assessment", err="Unknown requirement.")
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
        others = [s for s in db.execute(select(Score).where(Score.not_applicable.is_(True))).scalars().all() if s.requirement_id != rid]
        if others:
            errors.append(f"Only one requirement may be not applicable at BASIC; {others[0].requirement_id} already is.")
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
    score.updated_by = user.email
    db.commit()
    after = {"doc": score.doc_score, "impl": score.impl_score, "na": score.not_applicable}
    if before != after:
        log_activity(db, user.email, "score_update", "score", rid, {"before": before, "after": after})
    else:
        log_activity(db, user.email, "justification_update", "score", rid, {})
    if go == "next":
        _, nxt = fw.neighbours(rid)
        if nxt:
            return redirect(f"/assessment/{nxt.id}", msg=f"{rid} saved.")
    return redirect(f"/assessment/{rid}", msg="Saved.")


@router.post("/{rid}/evidence")
def add_evidence(
    request: Request,
    rid: str,
    title: str = Form(""),
    description: str = Form(""),
    url: str = Form(""),
    file: UploadFile | None = File(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    fw = load_framework()
    if fw.get(rid) is None:
        return redirect("/assessment", err="Unknown requirement.")
    settings = get_settings()
    ev = Evidence(title=title.strip()[:300], description=description.strip(), requirement_ids=[rid], collected_on=date.today(), collected_by=user.email)
    if file is not None and file.filename:
        try:
            stored, original, digest, size = store_upload(settings, file)
        except ValueError as exc:
            return redirect(f"/assessment/{rid}", err=str(exc))
        ev.kind = "file"
        ev.stored_name, ev.file_name, ev.sha256, ev.size = stored, original, digest, size
        ev.mime = (file.content_type or "")[:100]
        ev.title = ev.title or original
    elif url.strip():
        if not url.strip().lower().startswith(("https://", "http://")):
            return redirect(f"/assessment/{rid}", err="Links must start with https:// or http://.")
        ev.kind = "link"
        ev.url = url.strip()[:1000]
        ev.title = ev.title or ev.url
    else:
        return redirect(f"/assessment/{rid}", err="Provide a file or a link.")
    db.add(ev)
    db.commit()
    log_activity(db, user.email, "evidence_add", "evidence", str(ev.id), {"title": ev.title, "requirements": [rid], "sha256": ev.sha256})
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
    from ..services import parse_date

    fw = load_framework()
    if fw.get(rid) is None:
        return redirect("/assessment", err="Unknown requirement.")
    a = Action(
        title=title.strip()[:300] or "Untitled action",
        requirement_id=rid,
        owner=owner.strip()[:200],
        due_date=parse_date(due_date),
        priority=priority if priority in ("high", "medium", "low") else "medium",
    )
    db.add(a)
    db.commit()
    log_activity(db, user.email, "action_create", "action", str(a.id), {"title": a.title, "requirement": rid})
    return redirect(f"/assessment/{rid}", msg="Action added.")
