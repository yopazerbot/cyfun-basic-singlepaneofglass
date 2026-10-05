"""Remediation actions."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_user
from ..db import get_db
from ..framework import all_requirement_ids
from ..models import Action, User
from ..services import current_framework, log_activity, parse_date
from ..views import redirect, render

router = APIRouter(prefix="/actions", tags=["actions"])
STATUSES = ("open", "in_progress", "done", "cancelled")
PRIORITIES = ("high", "medium", "low")


def _ctx(db: Session, status: str, edit: Action | None):
    stmt = select(Action)
    if status in STATUSES:
        stmt = stmt.where(Action.status == status)
    elif status == "active":
        stmt = stmt.where(Action.status.in_(["open", "in_progress"]))
    items = db.execute(stmt.order_by(Action.status, Action.due_date.is_(None), Action.due_date, Action.priority)).scalars().all()
    return {"active": "actions", "items": items, "fw": current_framework(db), "statuses": STATUSES, "priorities": PRIORITIES, "f_status": status, "edit": edit}


@router.get("")
def list_actions(request: Request, status: str = "active", user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "actions.html", _ctx(db, status, None))


@router.get("/{action_id}")
def edit_action(request: Request, action_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    a = db.get(Action, action_id)
    if a is None:
        return redirect("/actions", err="Action not found.")
    return render(request, "actions.html", _ctx(db, "all", a))


def _apply(a: Action, form) -> None:
    a.title = (form.get("title") or "").strip()[:300] or a.title or "Untitled action"
    rid = form.get("requirement_id") or ""
    a.requirement_id = rid if rid in all_requirement_ids() else ""
    a.description = (form.get("description") or "").strip()
    a.owner = (form.get("owner") or "").strip()[:200]
    a.priority = form.get("priority") if form.get("priority") in PRIORITIES else "medium"
    new_status = form.get("status") if form.get("status") in STATUSES else "open"
    if new_status == "done" and a.status != "done":
        a.completed_on = parse_date(form.get("completed_on")) or date.today()
    elif new_status != "done":
        a.completed_on = None
    a.status = new_status
    a.due_date = parse_date(form.get("due_date"))


@router.post("")
async def create_action(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    if not (form.get("title") or "").strip():
        return redirect("/actions", err="Title is required.")
    a = Action(title="Untitled action")
    _apply(a, form)
    db.add(a)
    db.commit()
    log_activity(db, user.label, "action_create", "action", str(a.id), {"title": a.title, "requirement": a.requirement_id})
    return redirect("/actions", msg="Action added.")


@router.post("/{action_id}")
async def update_action(request: Request, action_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(Action, action_id)
    if a is None:
        return redirect("/actions", err="Action not found.")
    form = await request.form()
    _apply(a, form)
    db.commit()
    log_activity(db, user.label, "action_update", "action", str(a.id), {"title": a.title, "status": a.status})
    back = form.get("back") or "/actions"
    if not back.startswith("/") or back.startswith("//"):
        back = "/actions"
    return redirect(back, msg="Action updated.")


@router.post("/{action_id}/delete")
def delete_action(request: Request, action_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(Action, action_id)
    if a is not None:
        db.delete(a)
        db.commit()
        log_activity(db, user.label, "action_delete", "action", str(action_id), {"title": a.title})
    return redirect("/actions", msg="Action deleted.")
