"""Template environment, filters and the render helper shared by all routers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__
from . import db as database
from .config import get_settings

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def fmt_score(value, digits: int = 2) -> str:
    """Decimal comma, as used in the CCB material (2,50)."""
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}".replace(".", ",")


def fmt_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:16].replace("T", " ")


def status_word(value: float | None, target: float = 2.5) -> str:
    if value is None:
        return "none"
    return "ok" if float(value) >= float(target) else "low"


def pct(value: float | None, scale: float = 5.0) -> int:
    if value is None:
        return 0
    return max(0, min(100, round(100 * float(value) / scale)))


def nl2br_safe(value: str) -> str:
    from markupsafe import Markup, escape

    return Markup("<br>".join(escape(value or "").split("\n")))  # noqa: S704 - every fragment is escaped


templates.env.filters["score"] = fmt_score
templates.env.filters["date"] = fmt_date
templates.env.filters["status_word"] = status_word
templates.env.filters["pct"] = pct
templates.env.filters["nl2br"] = nl2br_safe
templates.env.globals["version"] = __version__


def _current_level() -> str:
    try:
        from .models import Organisation

        with database.session() as s:
            org = s.get(Organisation, 1)
            return (org.target_level if org and org.target_level else "BASIC").upper()
    except Exception:  # noqa: BLE001 - rendering must not fail because of a lookup
        return "BASIC"


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {
        "user": getattr(request.state, "user", None),
        "settings": get_settings(),
        "active": "",
        "today": date.today(),
        "msg": request.query_params.get("msg", ""),
        "err": request.query_params.get("err", ""),
        "level": _current_level(),
        "now_utc": datetime.now(UTC).replace(tzinfo=None),
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    if msg:
        path = f"{path}{sep}msg={quote(msg)}"
    elif err:
        path = f"{path}{sep}err={quote(err)}"
    return RedirectResponse(path, status_code=303)
