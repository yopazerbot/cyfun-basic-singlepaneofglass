"""Template environment, filters and the render helper shared by all routers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

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
    return Markup("<br>".join(escape(value or "").split("\n")))  # noqa: S704 - every fragment is escaped


def fmt_usd(value) -> str:
    return "USD " + fmt_score(value or 0.0, 2)


templates.env.filters["score"] = fmt_score
templates.env.filters["usd"] = fmt_usd
templates.env.filters["date"] = fmt_date
templates.env.filters["status_word"] = status_word
templates.env.filters["pct"] = pct
templates.env.filters["nl2br"] = nl2br_safe
templates.env.globals["version"] = __version__


_FLASH_KEY = secrets.token_bytes(32)  # per process; a restart only drops messages in flight


def _flash_sig(kind: str, text: str) -> str:
    return hmac.new(_FLASH_KEY, f"{kind}:{text}".encode(), hashlib.sha256).hexdigest()[:32]


def _flash(request: Request) -> tuple[str, str]:
    """Messages travel in the redirect URL but are shown only with a valid server signature,
    so a crafted link cannot put arbitrary text into the application's own message banner."""
    sig = request.query_params.get("s", "")
    msg = request.query_params.get("msg", "")
    err = request.query_params.get("err", "")
    if msg and hmac.compare_digest(sig, _flash_sig("msg", msg)):
        return msg, ""
    if err and hmac.compare_digest(sig, _flash_sig("err", err)):
        return "", err
    return "", ""


def _current_level() -> str:
    try:
        from .models import Organisation

        with database.session() as s:
            org = s.get(Organisation, 1)
            return (org.target_level if org and org.target_level else "BASIC").upper()
    except Exception:  # noqa: BLE001 - rendering must not fail because of a lookup
        return "BASIC"


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    msg, err = _flash(request)
    ctx = {
        "user": getattr(request.state, "user", None),
        "settings": get_settings(),
        "active": "",
        "today": date.today(),
        "msg": msg,
        "err": err,
        "level": _current_level(),
        "now_utc": datetime.now(UTC).replace(tzinfo=None),
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    """303 to `path`; the signed message goes into the query string, before any #fragment."""
    path, hash_, fragment = path.partition("#")
    sep = "&" if "?" in path else "?"
    if msg:
        path = f"{path}{sep}msg={quote(msg)}&s={_flash_sig('msg', msg)}"
    elif err:
        path = f"{path}{sep}err={quote(err)}&s={_flash_sig('err', err)}"
    return RedirectResponse(path + hash_ + fragment, status_code=303)
