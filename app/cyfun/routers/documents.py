"""Document register: policies, procedures, plans and records that carry documentation maturity."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_user
from ..db import get_db
from ..models import Document, User
from ..services import current_framework, log_activity, parse_date, valid_requirement_ids
from ..views import redirect, render

router = APIRouter(prefix="/documents", tags=["documents"])
TYPES = ("policy", "procedure", "plan", "register", "record", "other")
STATUSES = ("draft", "approved", "retired")


def _ctx(db: Session, edit: Document | None):
    docs = db.execute(select(Document).order_by(Document.status, Document.title)).scalars().all()
    return {"active": "documents", "docs": docs, "types": TYPES, "statuses": STATUSES, "fw": current_framework(db), "edit": edit}


@router.get("")
def list_docs(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "documents.html", _ctx(db, None))


@router.get("/{doc_id}")
def edit_doc(request: Request, doc_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    d = db.get(Document, doc_id)
    if d is None:
        return redirect("/documents", err="Document not found.")
    return render(request, "documents.html", _ctx(db, d))


def _apply(d: Document, form) -> None:
    d.title = (form.get("title") or "").strip()[:300] or d.title or "Untitled"
    d.doc_type = form.get("doc_type") if form.get("doc_type") in TYPES else "other"
    d.owner = (form.get("owner") or "").strip()[:200]
    d.version = (form.get("version") or "").strip()[:50]
    d.status = form.get("status") if form.get("status") in STATUSES else "draft"
    d.approved_by = (form.get("approved_by") or "").strip()[:200]
    d.approved_on = parse_date(form.get("approved_on"))
    d.last_review = parse_date(form.get("last_review"))
    d.next_review = parse_date(form.get("next_review"))
    link = (form.get("link") or "").strip()[:1000]
    d.link = link if (not link or link.lower().startswith(("https://", "http://"))) else ""
    d.requirement_ids = valid_requirement_ids(form.getlist("requirement_ids"))
    d.notes = (form.get("notes") or "").strip()


@router.post("")
async def create_doc(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    if not (form.get("title") or "").strip():
        return redirect("/documents", err="Title is required.")
    d = Document(title="Untitled")
    _apply(d, form)
    db.add(d)
    db.commit()
    log_activity(db, user.label, "document_create", "document", str(d.id), {"title": d.title, "status": d.status})
    return redirect("/documents", msg="Document added.")


@router.post("/{doc_id}")
async def update_doc(request: Request, doc_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    d = db.get(Document, doc_id)
    if d is None:
        return redirect("/documents", err="Document not found.")
    form = await request.form()
    _apply(d, form)
    db.commit()
    log_activity(db, user.label, "document_update", "document", str(d.id), {"title": d.title, "status": d.status, "version": d.version})
    return redirect("/documents", msg="Document updated.")


@router.post("/{doc_id}/delete")
def delete_doc(request: Request, doc_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    d = db.get(Document, doc_id)
    if d is not None:
        db.delete(d)
        db.commit()
        log_activity(db, user.label, "document_delete", "document", str(doc_id), {"title": d.title})
    return redirect("/documents", msg="Document deleted.")
