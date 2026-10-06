"""Backup and restore page: download a backup, restore one, run the scheduled backup now. Administrators only."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .. import backup, scheduler
from ..appsettings import load_config
from ..auth import require_admin
from ..config import get_settings
from ..db import get_db
from ..models import Activity, User
from ..onedrive import OneDrive
from ..secretbox import key_id
from ..services import log_activity
from ..views import redirect, render

router = APIRouter(prefix="/backup", tags=["backup"])
CONFIRM = "RESTORE"


@router.get("")
def backup_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    settings = get_settings()
    config = load_config(db, settings)
    recent = db.execute(select(Activity).where(Activity.entity == "backup").order_by(Activity.id.desc()).limit(15)).scalars().all()
    files = [{"name": p.name, "size": p.stat().st_size} for p in backup.local_backups(settings)]
    return render(
        request,
        "backup.html",
        {
            "active": "backup",
            "config": config,
            "onedrive": OneDrive(config).configured(),
            "files": files,
            "recent": recent,
            "key_problem": settings.secret_key_problem,
            "key_id": "" if settings.secret_key_problem else key_id(settings),
            "confirm": CONFIRM,
        },
    )


@router.get("/download")
def download(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    settings = get_settings()
    try:
        with tempfile.NamedTemporaryFile(dir=settings.tmp_dir, suffix=backup.SUFFIX, delete=False) as fh:
            path = Path(fh.name)
            manifest = backup.write_backup(settings, fh, user.label)
    except backup.BackupError as exc:
        path.unlink(missing_ok=True)
        return redirect("/backup", err=str(exc))
    log_activity(db, user.label, "backup_downloaded", "backup", "", {"bytes": path.stat().st_size, "files": len(manifest["files"])})
    return FileResponse(path, media_type="application/octet-stream", filename=backup.backup_name(), background=BackgroundTask(path.unlink, missing_ok=True))


@router.post("/run")
def run_now(request: Request, user: User = Depends(require_admin)):
    settings = get_settings()
    if settings.secret_key_problem:
        return redirect("/backup", err=f"Backups are encrypted with the server key: {settings.secret_key_problem}")
    scheduler.submit(scheduler.run_backup, settings, user.label, job_id="manual-backup")
    return redirect("/backup", msg="Backup started. The result appears below; reload the page in a moment.")


@router.get("/files/{name}")
def download_local(request: Request, name: str, user: User = Depends(require_admin)):
    path = backup.local_backup(get_settings(), name)
    if path is None:
        return redirect("/backup", err="Backup not found.")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


def _restore(request: Request, user: User, path: Path, label: str, form) -> object:
    if (form.get("confirm") or "").strip() != CONFIRM:
        return redirect("/backup#restore", err=f"Type {CONFIRM} to confirm the restore.")
    settings = get_settings()
    try:
        manifest, before = backup.restore(settings, path, user.label, load_config(settings=settings).backup_keep)
    except backup.BackupError as exc:
        return redirect("/backup#restore", err=f"Nothing was changed: {exc}")
    saved = f" The previous state is saved as {before.name}." if before else ""
    # The restored database has its own users and sessions; the current session may no longer exist.
    return redirect("/backup", msg=f"Restored {label} (made {manifest['created_at']} by {manifest['created_by']}).{saved}")


@router.post("/restore")
async def restore_upload(request: Request, file: UploadFile, user: User = Depends(require_admin)):
    form = await request.form()
    settings = get_settings()
    with tempfile.NamedTemporaryFile(dir=settings.tmp_dir, suffix=backup.SUFFIX, delete=False) as fh:
        shutil.copyfileobj(file.file, fh, backup.CHUNK)
        path = Path(fh.name)
    try:
        return _restore(request, user, path, file.filename or "the uploaded backup", form)
    finally:
        path.unlink(missing_ok=True)


@router.post("/files/{name}/restore")
async def restore_local(request: Request, name: str, user: User = Depends(require_admin)):
    path = backup.local_backup(get_settings(), name)
    if path is None:
        return redirect("/backup", err="Backup not found.")
    return _restore(request, user, path, path.name, await request.form())
