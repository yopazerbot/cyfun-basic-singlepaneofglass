"""Settings managed in the application: connector credentials, the connector schedule and Claude.

Every setting also has an environment variable. A non-empty environment value wins and the
field is shown read-only, so a deployment configured through .env keeps working. Otherwise the
value saved on the Settings page applies, then the default. Secrets are encrypted with
CYFUN_SECRET_KEY (secretbox.py); the page can replace or clear them but never shows them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as database
from .config import Settings, get_settings
from .models import AppSetting
from .secretbox import SecretBoxError, decrypt, encrypt

log = logging.getLogger("cyfun.settings")

GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
AI_MODELS = {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5"}
AI_EFFORTS = {"low": "Low", "medium": "Medium", "high": "High"}
MAX_SECRET_LENGTH = 4000


@dataclass(frozen=True)
class SettingField:
    name: str  # attribute on Config, and on Settings for the environment override
    group: str
    label: str
    env: str
    secret: bool = False
    kind: str = "text"  # text | int | choice | bool
    default: object = ""
    choices: dict = field(default_factory=dict)
    minimum: int | None = None
    maximum: int | None = None
    pattern: str = ""
    pattern_hint: str = ""
    help: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    help: str
    connector: str = ""  # connector key when the group configures a connector


GROUPS: tuple[Group, ...] = (
    Group(
        "microsoft",
        "Microsoft 365 / Entra ID",
        "App registration with read-only Microsoft Graph application permissions and admin consent, separate from the sign-in app registration.",
        "microsoft",
    ),
    Group(
        "github",
        "GitHub",
        "Fine-grained or classic token with read access. Without an organisation the connector reads the token user's own repositories.",
        "github",
    ),
    Group("railway", "Railway", "Account or team token.", "railway"),
    Group("cloudflare", "Cloudflare", "API token with read permissions. Account-level checks also need the account ID.", "cloudflare"),
    Group("schedule", "Connector schedule", "Every configured connector runs automatically at this interval."),
    Group(
        "claude",
        "Claude (Anthropic API)",
        "Claude proposes documentation and implementation scores per requirement. A proposal changes nothing until an administrator accepts it.",
    ),
)
GROUP_KEYS = {g.key: g for g in GROUPS}

FIELDS: tuple[SettingField, ...] = (
    SettingField("ms_graph_tenant_id", "microsoft", "Directory (tenant) ID", "MS_GRAPH_TENANT_ID", pattern=GUID, pattern_hint="a GUID"),
    SettingField("ms_graph_client_id", "microsoft", "Application (client) ID", "MS_GRAPH_CLIENT_ID", pattern=GUID, pattern_hint="a GUID"),
    SettingField("ms_graph_client_secret", "microsoft", "Client secret", "MS_GRAPH_CLIENT_SECRET", secret=True),
    SettingField("github_token", "github", "Token", "GITHUB_TOKEN", secret=True),
    SettingField(
        "github_org",
        "github",
        "Organisation (optional)",
        "GITHUB_ORG",
        pattern=r"[A-Za-z0-9][A-Za-z0-9-]{0,38}",
        pattern_hint="a GitHub organisation name",
        placeholder="empty: the token user's repositories",
    ),
    SettingField("railway_token", "railway", "API token", "RAILWAY_TOKEN", secret=True),
    SettingField("cloudflare_api_token", "cloudflare", "API token", "CLOUDFLARE_API_TOKEN", secret=True),
    SettingField(
        "cloudflare_account_id",
        "cloudflare",
        "Account ID (optional)",
        "CLOUDFLARE_ACCOUNT_ID",
        pattern=r"[0-9a-fA-F]{32}",
        pattern_hint="32 hexadecimal characters",
    ),
    SettingField("connector_sync_hours", "schedule", "Hours between automatic runs", "CONNECTOR_SYNC_HOURS", kind="int", default=24, minimum=1, maximum=168),
    SettingField(
        "anthropic_api_key",
        "claude",
        "Anthropic API key",
        "ANTHROPIC_API_KEY",
        secret=True,
        help="Created in the Claude Console. A separate workspace with its own spend limit keeps this use apart.",
    ),
    SettingField("ai_model", "claude", "Model", "AI_MODEL", kind="choice", default="claude-opus-5-5", choices=AI_MODELS),
    SettingField(
        "ai_effort",
        "claude",
        "Reasoning effort",
        "AI_EFFORT",
        kind="choice",
        default="medium",
        choices=AI_EFFORTS,
        help="Higher effort reasons longer per requirement and costs more.",
    ),
    SettingField(
        "ai_monthly_cap_usd",
        "claude",
        "Monthly spend limit (USD)",
        "AI_MONTHLY_CAP_USD",
        kind="int",
        default=25,
        minimum=0,
        maximum=10000,
        help="No new review starts once the estimated spend of the calendar month would pass this amount. 0 stops all reviews.",
    ),
    SettingField(
        "ai_review_after_sync",
        "claude",
        "Review changed requirements after each scheduled connector run",
        "AI_REVIEW_AFTER_SYNC",
        kind="bool",
        default=False,
        help="Submits a batch for the requirements whose documents, evidence or checks changed since their last review.",
    ),
)
BY_NAME = {f.name: f for f in FIELDS}


class Config:
    """Effective values. `sources[name]` is env, app, default or unreadable (a stored secret that cannot be decrypted)."""

    def __init__(self, values: dict, sources: dict, rows: dict[str, AppSetting], problems: dict[str, str]):
        self._values = values
        self.sources = sources
        self.rows = rows
        self.problems = problems

    def __getattr__(self, name: str):
        values = self.__dict__.get("_values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)


# --------------------------------------------------------------------------- reading
def _env_value(settings: Settings, f: SettingField):
    """The environment override, or None when the variable is unset, empty or invalid."""
    v = getattr(settings, f.name, None)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if f.kind == "choice":
        v = str(v).strip()
        if v not in f.choices:
            log.warning("%s=%s is not one of %s; ignored", f.env, v, ", ".join(f.choices))
            return None
    if f.kind == "int":
        v = int(v)
        if f.minimum is not None:
            v = max(f.minimum, v)
        if f.maximum is not None:
            v = min(f.maximum, v)
    if f.kind == "text":
        v = str(v).strip()
    return v


def _parse_stored(f: SettingField, raw: str):
    if f.kind == "int":
        try:
            return int(raw)
        except ValueError:
            return f.default
    if f.kind == "bool":
        return raw == "1"
    if f.kind == "choice":
        return raw if raw in f.choices else f.default
    return raw


def load_config(db: Session | None = None, settings: Settings | None = None) -> Config:
    settings = settings or get_settings()
    own = db is None
    db = db or database.session()
    try:
        rows = {r.key: r for r in db.execute(select(AppSetting)).scalars().all()}
    finally:
        if own:
            db.close()
    values: dict = {}
    sources: dict[str, str] = {}
    problems: dict[str, str] = {}
    for f in FIELDS:
        env = _env_value(settings, f)
        if env is not None:
            values[f.name], sources[f.name] = env, "env"
            continue
        row = rows.get(f.name)
        if row is None:
            values[f.name], sources[f.name] = f.default, "default"
        elif f.secret:
            try:
                values[f.name], sources[f.name] = decrypt(settings, f.name, row.ciphertext, row.key_id), "app"
            except SecretBoxError as exc:
                values[f.name], sources[f.name] = "", "unreadable"
                problems[f.name] = str(exc)
        else:
            values[f.name], sources[f.name] = _parse_stored(f, row.value), "app"
    return Config(values, sources, rows, problems)


# --------------------------------------------------------------------------- writing
def _validate(f: SettingField, raw: str) -> tuple[str | None, str]:
    """Returns (value to store or None for 'back to default', error)."""
    if f.kind == "bool":
        return ("1" if raw == "1" else "0"), ""
    if raw == "":
        return None, ""
    if f.kind == "int":
        try:
            n = int(raw)
        except ValueError:
            return None, f"{f.label} must be a whole number."
        if (f.minimum is not None and n < f.minimum) or (f.maximum is not None and n > f.maximum):
            return None, f"{f.label} must be between {f.minimum} and {f.maximum}."
        return str(n), ""
    if f.kind == "choice":
        return (raw, "") if raw in f.choices else (None, f"{f.label}: unknown option.")
    if len(raw) > 300:
        return None, f"{f.label} is too long."
    if f.pattern and not re.fullmatch(f.pattern, raw):
        return None, f"{f.label} must be {f.pattern_hint}."
    return raw, ""


def save_group(db: Session, settings: Settings, group: str, form, actor: str) -> tuple[list[dict], list[str]]:
    """Apply one Settings form. Nothing is written when any field is invalid.

    Returns (changes for the activity log, errors). Changes never contain secret values."""
    rows = {r.key: r for r in db.execute(select(AppSetting)).scalars().all()}
    changes: list[dict] = []
    errors: list[str] = []
    writes: list[tuple[str, SettingField, str | None]] = []
    for f in (f for f in FIELDS if f.group == group):
        if _env_value(settings, f) is not None:
            continue  # set by the environment; the form shows it read-only
        row = rows.get(f.name)
        if f.secret:
            if form.get(f"{f.name}__clear") == "1":
                if row is not None:
                    writes.append(("delete", f, None))
                    changes.append({"setting": f.label, "change": "cleared"})
                continue
            new = (form.get(f.name) or "").strip()
            if not new:
                continue
            if len(new) > MAX_SECRET_LENGTH or any(ch.isspace() for ch in new):
                errors.append(f"{f.label}: paste the value without spaces or line breaks.")
                continue
            if settings.secret_key_problem:
                errors.append(f"{f.label} cannot be stored: {settings.secret_key_problem}")
                continue
            writes.append(("secret", f, new))
            changes.append({"setting": f.label, "change": "replaced" if row is not None else "set"})
            continue
        raw = form.get(f.name)
        value, err = _validate(f, (raw or "").strip() if f.kind != "bool" else (raw or ""))
        if err:
            errors.append(err)
            continue
        old = row.value if row is not None else None
        if value is None:
            if row is not None:
                writes.append(("delete", f, None))
                changes.append({"setting": f.label, "change": "reset to default", "before": old})
        elif value != old:
            writes.append(("value", f, value))
            changes.append({"setting": f.label, "change": "changed", "before": old, "after": value})
    if errors:
        return [], errors
    for op, f, value in writes:
        row = rows.get(f.name)
        if op == "delete":
            if row is not None:
                db.delete(row)
            continue
        if row is None:
            row = AppSetting(key=f.name)
            db.add(row)
        if op == "secret":
            row.ciphertext, row.key_id = encrypt(settings, f.name, value)
            row.hint = value[-4:] if len(value) >= 20 else ""
            row.value = ""
        else:
            row.value, row.ciphertext, row.key_id, row.hint = value, "", "", ""
        row.updated_by = actor
    db.commit()
    return changes, []


# --------------------------------------------------------------------------- display
def describe(config: Config, settings: Settings) -> list[dict]:
    """Groups and fields for the Settings page. Secret values are never included."""
    out = []
    for g in GROUPS:
        items = []
        for f in (f for f in FIELDS if f.group == g.key):
            src = config.sources[f.name]
            row = config.rows.get(f.name)
            item = {"f": f, "source": src, "locked": src == "env", "row": row, "problem": config.problems.get(f.name, "")}
            if not f.secret:
                item["value"] = getattr(config, f.name)
            items.append(item)
        out.append({"g": g, "items": items})
    return out
