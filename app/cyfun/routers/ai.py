"""Claude review: proposals per requirement, batch reviews, decisions. Administrators only.

Auditors never see proposals: they are working material, like remediation actions. The audit
view and the audit pack show which accepted scores came from a proposal and who accepted them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import scheduler
from ..ai import service
from ..auth import require_admin
from ..config import get_settings
from ..db import get_db
from ..models import AiBatch, AiProposal, Score, User
from ..services import current_framework
from ..views import redirect, render, templates

router = APIRouter(prefix="/ai", tags=["ai"])


def panel_context(db: Session, rid: str, prop: AiProposal | None) -> dict:
    fw = current_framework(db)
    return {"prop": prop, "rid": rid, "score": db.get(Score, rid), "fw": fw, "ai": service.status(db, get_settings())}


@router.get("")
def queue(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    settings = get_settings()
    service.recover_stale(db)
    fw = current_framework(db)
    ready = db.execute(select(AiProposal).where(AiProposal.status == "ready").order_by(AiProposal.requirement_id)).scalars().all()
    ready = [p for p in ready if p.requirement_id in fw.by_id]
    active = db.execute(select(AiProposal).where(AiProposal.status.in_(service.ACTIVE))).scalars().all()
    decided = db.execute(select(AiProposal).where(AiProposal.decision != "").order_by(AiProposal.decided_at.desc()).limit(15)).scalars().all()
    failed = db.execute(select(AiProposal).where(AiProposal.status.in_(["failed", "declined"])).order_by(AiProposal.id.desc()).limit(10)).scalars().all()
    batches = db.execute(select(AiBatch).order_by(AiBatch.id.desc()).limit(10)).scalars().all()
    ai = service.status(db, settings)
    preview = service.batch_preview(db, settings, fw) if ai["configured"] else None
    scores = {s.requirement_id: s for s in db.execute(select(Score)).scalars().all()}
    return render(
        request,
        "ai.html",
        {
            "active": "ai",
            "fw": fw,
            "ai": ai,
            "ready": ready,
            "active_props": active,
            "decided": decided,
            "failed": failed,
            "batches": batches,
            "preview": preview,
            "scores": scores,
            "open_batch": next((b for b in batches if b.status == "submitted"), None),
        },
    )


@router.post("/batch")
def submit_batch(request: Request, scope: str = Form("changed"), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        batch = service.submit_batch(db, get_settings(), scope, user.label)
    except service.AiError as exc:
        return redirect("/ai", err=str(exc))
    n = batch.request_count
    return redirect("/ai", msg=f"Batch submitted for {n} requirement{'' if n == 1 else 's'}. Results arrive within 24 hours, usually within an hour.")


@router.post("/batches/{bid}/check")
def check_batch(request: Request, bid: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    batch = db.get(AiBatch, bid)
    if batch is None:
        return redirect("/ai", err="Batch not found.")
    ended = service.poll_batches(get_settings(), only_id=bid)
    db.refresh(batch)
    if batch.error and batch.status == "submitted" and not batch.error.startswith("Cancel"):
        return redirect("/ai", err=batch.error)
    return redirect("/ai", msg="Results collected." if ended else "The batch is still being processed.")


@router.post("/batches/{bid}/cancel")
def cancel_batch(request: Request, bid: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    batch = db.get(AiBatch, bid)
    if batch is None:
        return redirect("/ai", err="Batch not found.")
    try:
        service.cancel_batch(db, get_settings(), batch, user.label)
    except service.AiError as exc:
        return redirect("/ai", err=str(exc))
    return redirect("/ai", msg="Cancel requested. Requests that already ran keep their results.")


@router.post("/review/{rid}")
def review(request: Request, rid: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    settings = get_settings()
    try:
        prop = service.start_review(db, settings, rid, user.label)
    except service.AiError as exc:
        return redirect(f"/assessment/{rid}#claude", err=str(exc))
    scheduler.submit(service.run_review, settings, prop.id, job_id=f"ai-review-{prop.id}")
    return redirect(f"/assessment/{rid}#claude", msg=f"Claude is reviewing {rid}. The proposal appears on this page when it is ready.")


def _proposal(db: Session, pid: int) -> AiProposal | None:
    return db.get(AiProposal, pid)


@router.get("/proposals/{pid}")
def proposal_page(request: Request, pid: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    prop = _proposal(db, pid)
    if prop is None:
        return redirect("/ai", err="Proposal not found.")
    ctx = panel_context(db, prop.requirement_id, prop)
    ctx.update({"active": "ai", "standalone": True})
    return render(request, "ai_proposal.html", ctx)


@router.get("/proposals/{pid}/panel")
def proposal_panel(request: Request, pid: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    prop = _proposal(db, pid)
    if prop is None:
        return redirect("/ai", err="Proposal not found.")
    service.recover_stale(db)
    db.refresh(prop)
    ctx = panel_context(db, prop.requirement_id, service.latest_for(db, prop.requirement_id))
    ctx.update({"request": request, "user": user})
    return templates.TemplateResponse(request, "partials/ai_panel.html", ctx)


@router.post("/proposals/{pid}/accept")
async def accept(request: Request, pid: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    prop = _proposal(db, pid)
    if prop is None:
        return redirect("/ai", err="Proposal not found.")
    form = await request.form()
    indexes = []
    for v in form.getlist("action"):
        try:
            indexes.append(int(v))
        except ValueError:
            continue
    try:
        created = service.accept(
            db, current_framework(db), prop, form.get("doc_score", ""), form.get("impl_score", ""), form.get("justification", ""), indexes, user.label
        )
    except service.AiError as exc:
        return redirect(f"/assessment/{prop.requirement_id}#claude", err=str(exc))
    extra = f" {len(created)} action(s) created." if created else ""
    return redirect(f"/assessment/{prop.requirement_id}", msg=f"Scores of {prop.requirement_id} saved from Claude's proposal.{extra}")


@router.post("/proposals/{pid}/reject")
def reject(request: Request, pid: int, note: str = Form(""), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    prop = _proposal(db, pid)
    if prop is None:
        return redirect("/ai", err="Proposal not found.")
    try:
        service.reject(db, prop, note, user.label)
    except service.AiError as exc:
        return redirect(f"/assessment/{prop.requirement_id}#claude", err=str(exc))
    return redirect(f"/assessment/{prop.requirement_id}", msg="Proposal rejected. The scores are unchanged.")
