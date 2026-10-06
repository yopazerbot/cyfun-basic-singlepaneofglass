"""Assurance level risk assessment (CCB CyFun-Selection) and the simple risk register."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import risk as riskmod
from ..auth import require_admin, require_user
from ..db import get_db
from ..framework import load_risk_model
from ..models import RiskAssessment, RiskItem, User
from ..services import get_risk, log_activity, parse_date, parse_int
from ..views import redirect, render

router = APIRouter(prefix="/risk", tags=["risk"])
TREATMENTS = ("mitigate", "accept", "transfer", "avoid")
STATUSES = ("open", "treated", "accepted", "closed")


def _first_sector(model: dict) -> str:
    return model["sectors"][0]["id"]


def _known_sector(model: dict, sector_id: str) -> bool:
    return any(s["id"] == sector_id for s in model["sectors"])


def _matrix_from_form(form, model: dict, sector_id: str) -> dict:
    base = riskmod.default_matrix(model, sector_id)
    for i, row in enumerate(base["rows"]):
        imp = form.get(f"impact_{i}")
        if imp in riskmod.LEVELS:
            row["impact"] = imp
        for j in range(len(row["probability"])):
            p = form.get(f"prob_{i}_{j}")
            if p in riskmod.LEVELS:
                row["probability"][j] = p
    return base


def _from_form(form, model: dict) -> tuple[str, int, dict]:
    """Sector, organisation size and threat matrix as posted by the risk form."""
    sector_id = form.get("sector_id") or _first_sector(model)
    size = parse_int(form.get("organisation_size"), 1, 3) or 1
    return sector_id, size, _matrix_from_form(form, model, sector_id)


@router.get("")
def risk_page(request: Request, sector: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    model = load_risk_model()
    ra = get_risk(db)
    if sector and sector != (ra.sector_id if ra else ""):
        sector_id = sector if _known_sector(model, sector) else _first_sector(model)
        size = ra.organisation_size if ra else 1
        matrix = riskmod.default_matrix(model, sector_id)
        rationale = ra.rationale if ra else ""
    elif ra and ra.matrix.get("rows"):
        sector_id, size, matrix, rationale = ra.sector_id, ra.organisation_size, ra.matrix, ra.rationale
    else:
        sector_id = _first_sector(model)
        size, matrix, rationale = 1, riskmod.default_matrix(model, sector_id), ""
    return render(
        request,
        "risk.html",
        {
            "active": "risk",
            "model": model,
            "sector": riskmod.sector(model, sector_id),
            "sector_id": sector_id,
            "size": size,
            "matrix": matrix,
            "rationale": rationale,
            "result": riskmod.compute(model, matrix, size),
            "levels": riskmod.LEVELS,
            "sizes": riskmod.SIZES,
            "saved": ra,
            "defaults": riskmod.default_matrix(model, sector_id),
        },
    )


@router.post("/preview")
async def risk_preview(request: Request, user: User = Depends(require_user)):
    model = load_risk_model()
    sector_id, size, matrix = _from_form(await request.form(), model)
    result = riskmod.compute(model, matrix, size)
    return render(request, "partials/risk_result.html", {"result": result, "model": model, "sector": riskmod.sector(model, sector_id), "size": size})


@router.post("")
async def risk_save(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    model = load_risk_model()
    form = await request.form()
    sector_id, size, matrix = _from_form(form, model)
    if not _known_sector(model, sector_id):
        return redirect("/risk", err="Unknown sector.")
    errors = riskmod.validate_matrix(matrix)
    if errors:
        return redirect("/risk", err=" ".join(errors))
    result = riskmod.compute(model, matrix, size)
    ra = get_risk(db)
    if ra is None:
        ra = RiskAssessment()
        db.add(ra)
    ra.sector_id = sector_id
    ra.organisation_size = size
    ra.matrix = matrix
    ra.rationale = (form.get("rationale") or "").strip()
    ra.total_score = result.total
    ra.level = result.level
    ra.updated_by = user.label
    db.commit()
    log_activity(
        db, user.label, "risk_assessment_save", "risk_assessment", str(ra.id), {"sector": sector_id, "size": size, "total": result.total, "level": result.level}
    )
    return redirect("/risk", msg=f"Risk assessment saved. Score {result.total:g}, assurance level {result.level}.")


# --------------------------------------------------------------------------- register
def _register_page(request: Request, db: Session, edit: RiskItem | None):
    items = db.execute(select(RiskItem).order_by(RiskItem.status, (RiskItem.likelihood * RiskItem.impact).desc(), RiskItem.id)).scalars().all()
    return render(request, "risk_register.html", {"active": "risk", "items": items, "treatments": TREATMENTS, "statuses": STATUSES, "edit": edit})


@router.get("/register")
def register(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _register_page(request, db, None)


@router.get("/register/{item_id}")
def register_edit(request: Request, item_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    item = db.get(RiskItem, item_id)
    if item is None:
        return redirect("/risk/register", err="Risk not found.")
    return _register_page(request, db, item)


def _apply(item: RiskItem, form) -> None:
    item.title = (form.get("title") or "").strip()[:200] or item.title or "Untitled risk"
    item.threat = (form.get("threat") or "").strip()
    item.vulnerability = (form.get("vulnerability") or "").strip()
    item.assets = (form.get("assets") or "").strip()[:300]
    item.likelihood = parse_int(form.get("likelihood"), 1, 3) or 2
    item.impact = parse_int(form.get("impact"), 1, 3) or 2
    item.treatment = form.get("treatment") if form.get("treatment") in TREATMENTS else "mitigate"
    item.measures = (form.get("measures") or "").strip()
    item.owner = (form.get("owner") or "").strip()[:200]
    item.status = form.get("status") if form.get("status") in STATUSES else "open"
    item.review_date = parse_date(form.get("review_date"))


@router.post("/register")
async def register_create(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    item = RiskItem(title="Untitled risk")
    _apply(item, form)
    db.add(item)
    db.commit()
    log_activity(db, user.label, "risk_item_create", "risk_item", str(item.id), {"title": item.title})
    return redirect("/risk/register", msg="Risk added.")


@router.post("/register/{item_id}")
async def register_update(request: Request, item_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(RiskItem, item_id)
    if item is None:
        return redirect("/risk/register", err="Risk not found.")
    form = await request.form()
    _apply(item, form)
    db.commit()
    log_activity(db, user.label, "risk_item_update", "risk_item", str(item.id), {"title": item.title, "status": item.status})
    return redirect("/risk/register", msg="Risk updated.")


@router.post("/register/{item_id}/delete")
def register_delete(request: Request, item_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(RiskItem, item_id)
    if item is not None:
        db.delete(item)
        db.commit()
        log_activity(db, user.label, "risk_item_delete", "risk_item", str(item_id), {"title": item.title})
    return redirect("/risk/register", msg="Risk deleted.")
