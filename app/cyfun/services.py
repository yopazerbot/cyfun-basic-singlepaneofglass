"""Shared application services: activity log, evidence storage, current framework and assessment state."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .framework import Framework, all_requirement_ids, load_framework, normalise_level
from .models import Action, Activity, CheckResult, ConnectorRun, Document, Evidence, Organisation, RiskAssessment, Score
from .scoring import ReqInput, Summary, compute

ALLOWED_EXTENSIONS = set(".pdf .png .jpg .jpeg .gif .webp .txt .md .csv .json .xml .docx .xlsx .pptx .odt .ods .zip .log .eml .msg .html .yaml .yml".split())

JOURNEY_STAGES = [
    ("scope", "Scope, organisation and target level"),
    ("risk", "Risk assessment and assurance level"),
    ("assessment", "Self-assessment"),
    ("remediation", "Remediation"),
    ("evidence", "Evidence collection"),
    ("declaration", "Self-declaration (CCB workbook)"),
    ("verification", "Verification by the CAB"),
    ("label", "CyFun label"),
]


# --------------------------------------------------------------------------- activity
def log_activity(db: Session, actor: str, action: str, entity: str = "", entity_id: str = "", details: dict | None = None) -> None:
    db.add(Activity(actor=actor, action=action, entity=entity, entity_id=str(entity_id), details=details or {}))
    db.commit()


# --------------------------------------------------------------------------- organisation and framework
def get_org(db: Session) -> Organisation:
    org = db.get(Organisation, 1)
    if org is None:
        org = Organisation(id=1, name="")
        db.add(org)
        db.commit()
    return org


def current_level(db: Session) -> str:
    return normalise_level(get_org(db).target_level)


def current_framework(db: Session) -> Framework:
    return load_framework(current_level(db))


def valid_requirement_ids(ids: list[str]) -> list[str]:
    known = all_requirement_ids()
    return [r for r in ids if r in known]


def get_risk(db: Session) -> RiskAssessment | None:
    return db.execute(select(RiskAssessment).order_by(RiskAssessment.id.desc()).limit(1)).scalar_one_or_none()


# --------------------------------------------------------------------------- assessment
def scores_by_id(db: Session) -> dict[str, Score]:
    return {r.requirement_id: r for r in db.execute(select(Score)).scalars().all()}


def score_inputs(db: Session) -> dict[str, ReqInput]:
    return {rid: ReqInput(r.doc_score, r.impl_score, r.not_applicable) for rid, r in scores_by_id(db).items()}


def current_summary(db: Session, fw: Framework) -> Summary:
    return compute(fw, score_inputs(db))


# --------------------------------------------------------------------------- evidence files
def safe_filename(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r"[^\w.\- ()]", "_", name).strip() or "file"
    return name[:200]


def store_upload(settings: Settings, upload: UploadFile) -> tuple[str, str, str, int]:
    """Store an uploaded file under a random name. Returns (stored_name, original_name, sha256, size)."""
    original = safe_filename(upload.filename or "file")
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"File type {ext or '(none)'} is not accepted. Allowed: " + ", ".join(sorted(ALLOWED_EXTENSIONS)))
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)
    stored = secrets.token_hex(16) + ext
    limit = settings.max_upload_mb * 1024 * 1024
    h = hashlib.sha256()
    size = 0
    target = settings.evidence_dir / stored
    with target.open("wb") as fh:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                fh.close()
                target.unlink(missing_ok=True)
                raise ValueError(f"File exceeds the {settings.max_upload_mb} MB limit.")
            h.update(chunk)
            fh.write(chunk)
    return stored, original, h.hexdigest(), size


def evidence_path(settings: Settings, ev: Evidence) -> Path:
    p = (settings.evidence_dir / ev.stored_name).resolve()
    if settings.evidence_dir.resolve() not in p.parents:
        raise ValueError("invalid evidence path")
    return p


# --------------------------------------------------------------------------- connectors
def latest_runs(db: Session) -> dict[str, ConnectorRun]:
    sub = select(ConnectorRun.connector, func.max(ConnectorRun.id).label("mid")).group_by(ConnectorRun.connector).subquery()
    rows = db.execute(select(ConnectorRun).join(sub, ConnectorRun.id == sub.c.mid)).scalars().all()
    return {r.connector: r for r in rows}


def latest_checks(db: Session) -> list[CheckResult]:
    ids = [r.id for r in latest_runs(db).values() if r.status == "ok"]
    if not ids:
        return []
    return list(db.execute(select(CheckResult).where(CheckResult.run_id.in_(ids)).order_by(CheckResult.connector, CheckResult.check_id)).scalars().all())


# --------------------------------------------------------------------------- grouping helpers
def checks_by_requirement(checks: list[CheckResult]) -> dict[str, list[CheckResult]]:
    return by_requirement(checks)


def by_requirement(items, attr: str = "requirement_ids") -> dict[str, list]:
    out: dict[str, list] = {}
    for it in items:
        ids = getattr(it, attr) or []
        if isinstance(ids, str):
            ids = [ids]
        for rid in ids:
            out.setdefault(rid, []).append(it)
    return out


def open_actions(db: Session) -> list[Action]:
    return list(
        db.execute(select(Action).where(Action.status.in_(["open", "in_progress"])).order_by(Action.due_date.is_(None), Action.due_date)).scalars().all()
    )


def documents_due(db: Session, today: date | None = None) -> list[Document]:
    today = today or date.today()
    docs = db.execute(select(Document).where(Document.status != "retired")).scalars().all()
    return [d for d in docs if d.next_review and d.next_review <= today]


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_int(value: str | None, lo: int | None = None, hi: int | None = None) -> int | None:
    try:
        n = int(str(value).strip())  # None and "" fail here too
    except ValueError:
        return None
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        return None
    return n
