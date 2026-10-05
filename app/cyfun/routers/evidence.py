"""Evidence register: files and links mapped to requirements, with integrity hashes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_user
from ..config import get_settings
from ..db import get_db
from ..framework import load_framework
from ..models import Evidence, User
from ..services import evidence_path, log_activity, parse_date, store_upload
from ..views import redirect, render

router = APIRouter(prefix="/evidence", tags=["evidence"])


@router.get("")
def list_evidence(request: Request, requirement: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = load_framework()
    items = db.execute(select(Evidence).order_by(Evidence.id.desc())).scalars().all()
    if requirement:
        items = [e for e in items if requirement in (e.requirement_ids or [])]
    return render(request, "evidence.html", {"active": "evidence", "items": items, "fw": fw, "f_requirement": requirement, "edit": None})


@router.get("/{ev_id}")
def edit_evidence(request: Request, ev_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    fw = load_framework()
    ev = db.get(Evidence, ev_id)
    if ev is None:
        return redirect("/evidence", err="Evidence not found.")
    items = db.execute(select(Evidence).order_by(Evidence.id.desc())).scalars().all()
    return render(request, "evidence.html", {"active": "evidence", "items": items, "fw": fw, "f_requirement": "", "edit": ev})


@router.get("/{ev_id}/download")
def download(request: Request, ev_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ev = db.get(Evidence, ev_id)
    if ev is None or ev.kind != "file" or not ev.stored_name:
        return redirect("/evidence", err="No file for this evidence.")
    path = evidence_path(get_settings(), ev)
    if not path.exists():
        return redirect("/evidence", err="File missing on disk.")
    return FileResponse(path, filename=ev.file_name or ev.stored_name, media_type="application/octet-stream", headers={"X-Content-Type-Options": "nosniff"})


@router.post("")
async def create_evidence(request: Request, file: UploadFile | None = File(None), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    fw = load_framework()
    form = await request.form()
    settings = get_settings()
    reqs = [r for r in form.getlist("requirement_ids") if r in fw.by_id]
    ev = Evidence(
        title=(form.get("title") or "").strip()[:300],
        description=(form.get("description") or "").strip(),
        requirement_ids=reqs,
        collected_on=parse_date(form.get("collected_on")) or date.today(),
        collected_by=user.email,
    )
    url = (form.get("url") or "").strip()
    if file is not None and file.filename:
        try:
            stored, original, digest, size = store_upload(settings, file)
        except ValueError as exc:
            return redirect("/evidence", err=str(exc))
        ev.kind, ev.stored_name, ev.file_name, ev.sha256, ev.size = "file", stored, original, digest, size
        ev.mime = (file.content_type or "")[:100]
        ev.title = ev.title or original
    elif url:
        if not url.lower().startswith(("https://", "http://")):
            return redirect("/evidence", err="Links must start with https:// or http://.")
        ev.kind, ev.url = "link", url[:1000]
        ev.title = ev.title or url
    else:
        return redirect("/evidence", err="Provide a file or a link.")
    db.add(ev)
    db.commit()
    log_activity(db, user.email, "evidence_add", "evidence", str(ev.id), {"title": ev.title, "requirements": reqs, "sha256": ev.sha256})
    return redirect("/evidence", msg="Evidence added.")


@router.post("/{ev_id}")
async def update_evidence(request: Request, ev_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    fw = load_framework()
    ev = db.get(Evidence, ev_id)
    if ev is None:
        return redirect("/evidence", err="Evidence not found.")
    form = await request.form()
    ev.title = (form.get("title") or "").strip()[:300] or ev.title
    ev.description = (form.get("description") or "").strip()
    ev.requirement_ids = [r for r in form.getlist("requirement_ids") if r in fw.by_id]
    ev.collected_on = parse_date(form.get("collected_on")) or ev.collected_on
    if ev.kind == "link":
        url = (form.get("url") or "").strip()
        if url.lower().startswith(("https://", "http://")):
            ev.url = url[:1000]
    db.commit()
    log_activity(db, user.email, "evidence_update", "evidence", str(ev.id), {"title": ev.title, "requirements": ev.requirement_ids})
    return redirect("/evidence", msg="Evidence updated.")


@router.post("/{ev_id}/delete")
def delete_evidence(request: Request, ev_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    ev = db.get(Evidence, ev_id)
    if ev is None:
        return redirect("/evidence", err="Evidence not found.")
    if ev.kind == "file" and ev.stored_name:
        try:
            evidence_path(get_settings(), ev).unlink(missing_ok=True)
        except ValueError:
            pass
    db.delete(ev)
    db.commit()
    log_activity(db, user.email, "evidence_delete", "evidence", str(ev_id), {"title": ev.title, "sha256": ev.sha256})
    return redirect("/evidence", msg="Evidence deleted.")
