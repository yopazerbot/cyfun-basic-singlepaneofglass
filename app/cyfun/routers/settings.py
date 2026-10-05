"""Settings page: connector credentials, connector schedule and Claude. Administrators only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import scheduler
from ..ai import claude
from ..ai.service import status as ai_status
from ..appsettings import BY_NAME, FIELDS, GROUP_KEYS, Config, _validate, describe, load_config, save_group
from ..auth import require_admin
from ..config import get_settings
from ..connectors import registry
from ..connectors.base import Connector
from ..db import get_db
from ..models import User
from ..services import log_activity
from ..views import redirect, render

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def settings_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    settings = get_settings()
    config = load_config(db, settings)
    connectors = registry(config)
    return render(
        request,
        "settings.html",
        {
            "active": "settings",
            "groups": describe(config, settings),
            "key_problem": settings.secret_key_problem,
            "configured": {k: c.configured() for k, c in connectors.items()},
            "ai": ai_status(db, settings),
        },
    )


@router.post("/{group}")
async def save(request: Request, group: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if group not in GROUP_KEYS:
        return redirect("/settings", err="Unknown settings group.")
    settings = get_settings()
    form = await request.form()
    changes, errors = save_group(db, settings, group, form, user.label)
    if errors:
        return redirect(f"/settings#{group}", err=" ".join(errors))
    if not changes:
        return redirect(f"/settings#{group}", msg="Nothing changed.")
    log_activity(db, user.label, "settings_update", "settings", group, {"changes": changes})
    if group == "schedule":
        scheduler.reschedule(load_config(db, settings).connector_sync_hours)
    return redirect(f"/settings#{group}", msg=f"{GROUP_KEYS[group].title}: saved.")


def _with_form(config: Config, group: str, form) -> tuple[Config, bool]:
    """The saved configuration with the values typed in the form on top, for testing before saving."""
    values = dict(config.__dict__["_values"])
    typed = False
    for f in (f for f in FIELDS if f.group == group):
        if config.sources[f.name] == "env":
            continue
        raw = (form.get(f.name) or "").strip()
        if not raw or f.kind == "bool":
            continue
        if f.secret:
            values[f.name] = raw
            typed = True
            continue
        value, err = _validate(f, raw)
        if not err and value is not None:
            values[f.name] = int(value) if f.kind == "int" else value
            typed = typed or values[f.name] != getattr(config, f.name)
    return Config(values, config.sources, config.rows, config.problems), typed


@router.post("/{group}/test")
async def test(request: Request, group: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    g = GROUP_KEYS.get(group)
    if g is None or (not g.connector and group != "claude"):
        return redirect("/settings", err="This group has no connection test.")
    settings = get_settings()
    form = await request.form()
    config, typed = _with_form(load_config(db, settings), group, form)
    suffix = " The values typed in the form were used and are not saved yet." if typed else ""
    if group == "claude":
        if not config.anthropic_api_key:
            return redirect("/settings#claude", err="No Anthropic API key is set.")
        try:
            result = claude.test_key(config.anthropic_api_key, config.ai_model)
        except claude.ApiError as exc:
            return redirect("/settings#claude", err=f"Test failed: {exc}{suffix}")
        log_activity(db, user.label, "settings_test", "settings", group, {"result": "ok"})
        return redirect("/settings#claude", msg=result + suffix)
    connector = registry(config)[g.connector]
    if not connector.configured():
        missing = ", ".join(BY_NAME[n].label for n in BY_NAME if BY_NAME[n].group == group and BY_NAME[n].secret)
        return redirect(f"/settings#{group}", err=f"{g.title} is not configured: {missing} missing.")
    try:
        result = connector.test()
    except Exception as exc:  # noqa: BLE001 - shown to the administrator
        log_activity(db, user.label, "settings_test", "settings", group, {"result": "failed"})
        return redirect(f"/settings#{group}", err=f"Test failed: {Connector.describe_error(exc)}.{suffix}")
    log_activity(db, user.label, "settings_test", "settings", group, {"result": "ok"})
    return redirect(f"/settings#{group}", msg=result + suffix)
