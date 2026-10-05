"""Append-only activity log."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_user
from ..db import get_db
from ..models import Activity, User
from ..views import render

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("")
def activity(request: Request, page: int = 1, user: User = Depends(require_user), db: Session = Depends(get_db)):
    page = max(1, page)
    size = 100
    items = db.execute(select(Activity).order_by(Activity.id.desc()).offset((page - 1) * size).limit(size + 1)).scalars().all()
    has_next = len(items) > size
    return render(request, "activity.html", {"active": "activity", "items": items[:size], "page": page, "has_next": has_next})
