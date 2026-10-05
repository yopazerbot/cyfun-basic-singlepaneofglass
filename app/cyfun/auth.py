"""Sign-in: Microsoft Entra ID single sign-on and optional local password accounts.

Entra ID (OpenID Connect, authorization code flow with PKCE)
------------------------------------------------------------
1. GET /auth/microsoft stores state, nonce and a PKCE verifier in the database (10 minutes)
   and redirects to the tenant's authorization endpoint.
2. GET /auth/callback exchanges the code for an ID token (client secret + PKCE verifier),
   validates signature, issuer, audience, expiry, nonce and tenant, derives the role from
   the app roles claim, upserts the user and creates a server-side session.

Local accounts (AUTH_LOCAL_ENABLED, default on)
-----------------------------------------------
Username and password verified against a scrypt hash. A default "admin" account with
password "admin" is created at first start and must change its password before it can
do anything else. Five failed attempts lock the account for fifteen minutes. Admins
manage local accounts on /auth/users; Entra users are created at their first sign-in.

Sessions
--------
The browser holds only an opaque random session token in an HttpOnly cookie. The database
stores its SHA-256 hash. Sessions expire after an idle period and after an absolute lifetime.
Roles: "admin" (full access) and "auditor" (read-only).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import db as database
from .config import Settings, get_settings, new_token
from .db import get_db
from .models import LoginState, SessionRow, User, utcnow
from .passwords import hash_password, password_problems, temporary_password, verify_password

log = logging.getLogger("cyfun.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = "cyfun_session"
ROLES = ("admin", "auditor")
ROLE_MAP = {"admin": "admin", "auditor": "auditor"}
DEFAULT_ADMIN = ("admin", "admin")

_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, object]] = {}


class PasswordChangeRequired(Exception):  # noqa: N818 - control-flow signal handled by the app
    pass


# --------------------------------------------------------------------------- Entra helpers
def _authority(s: Settings) -> str:
    return f"https://login.microsoftonline.com/{s.auth_tenant_id}/v2.0"


def discovery(s: Settings) -> dict:
    key = s.auth_tenant_id
    now = time.time()
    cached = _discovery_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    r = httpx.get(f"{_authority(s)}/.well-known/openid-configuration", timeout=10)
    r.raise_for_status()
    doc = r.json()
    _discovery_cache[key] = (now + 3600, doc)
    return doc


def jwks(s: Settings, force: bool = False):
    key = s.auth_tenant_id
    now = time.time()
    cached = _jwks_cache.get(key)
    if cached and cached[0] > now and not force:
        return cached[1]
    r = httpx.get(discovery(s)["jwks_uri"], timeout=10)
    r.raise_for_status()
    keyset = JsonWebKey.import_key_set(r.json())
    _jwks_cache[key] = (now + 3600, keyset)
    return keyset


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _pkce_pair() -> tuple[str, str]:
    verifier = new_token(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _safe_next(path: str | None) -> str:
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


def role_from_claims(claims: dict, s: Settings) -> str | None:
    roles = claims.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    found = [ROLE_MAP[r.lower()] for r in roles if isinstance(r, str) and r.lower() in ROLE_MAP]
    if "admin" in found:
        return "admin"
    if "auditor" in found:
        return "auditor"
    default = s.auth_default_role.strip().lower()
    return default if default in ROLES else None


def validate_id_token(id_token: str, s: Settings, nonce: str) -> dict:
    claims_options = {
        "iss": {"essential": True, "values": [_authority(s)]},
        "aud": {"essential": True, "values": [s.auth_client_id]},
        "exp": {"essential": True},
        "nonce": {"essential": True, "value": nonce},
        "tid": {"essential": True, "value": s.auth_tenant_id},
    }
    try:
        claims = jwt.decode(id_token, jwks(s), claims_options=claims_options)
        claims.validate(leeway=60)
    except (JoseError, ValueError):
        claims = jwt.decode(id_token, jwks(s, force=True), claims_options=claims_options)
        claims.validate(leeway=60)
    return dict(claims)


# --------------------------------------------------------------------------- sessions
def create_session(db: Session, s: Settings, user: User, request: Request) -> str:
    token = new_token(32)
    now = utcnow()
    db.add(
        SessionRow(
            token_hash=_hash(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=s.session_absolute_hours),
            ip=(request.client.host if request.client else "")[:64],
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
    db.commit()
    return token


def set_cookie(response: Response, s: Settings, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=s.session_absolute_hours * 3600, httponly=True, secure=s.secure_cookies, samesite="lax", path="/")


def clear_cookie(response: Response, s: Settings) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=s.secure_cookies, samesite="lax")


def load_user(request: Request, db: Session, s: Settings) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.get(SessionRow, _hash(token))
    if row is None:
        return None
    now = utcnow()
    if now > row.expires_at or now > row.last_seen_at + timedelta(minutes=s.session_idle_minutes):
        db.delete(row)
        db.commit()
        return None
    user = db.get(User, row.user_id)
    if user is None or user.disabled or (user.auth_provider == "local" and not s.auth_local_enabled):
        db.delete(row)
        db.commit()
        return None
    if (now - row.last_seen_at).total_seconds() > 60:
        row.last_seen_at = now
        db.commit()
    return user


def end_all_sessions(db: Session, user_id: int) -> None:
    db.execute(delete(SessionRow).where(SessionRow.user_id == user_id))
    db.commit()


# --------------------------------------------------------------------------- dependencies
def current_user(request: Request, db: Session = Depends(get_db), s: Settings = Depends(get_settings)) -> User | None:
    user = load_user(request, db, s)
    request.state.user = user
    return user


# Paths an account with a pending password change may still use; everything else redirects
# to the password form, including the user-administration endpoints under /auth/users.
PASSWORD_CHANGE_EXEMPT = {"/auth/password", "/auth/logout", "/auth/signed-out"}


def require_user(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    if user.must_change_password and request.url.path not in PASSWORD_CHANGE_EXEMPT:
        raise PasswordChangeRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="This action needs the Admin role")
    return user


# --------------------------------------------------------------------------- local accounts
def ensure_default_admin(s: Settings) -> None:
    """Create admin/admin (password change forced) when local login is on and no local account exists."""
    if not s.auth_local_enabled:
        return
    db = database.session()
    try:
        exists = db.execute(select(User).where(User.auth_provider == "local")).first()
        if exists is None:
            username, password = DEFAULT_ADMIN
            if s.auth_bootstrap_password.strip():
                password = s.auth_bootstrap_password.strip()
            db.add(
                User(
                    oid=f"local:{username}",
                    email="",
                    display_name="Administrator",
                    role="admin",
                    auth_provider="local",
                    username=username,
                    password_hash=hash_password(password),
                    must_change_password=True,
                )
            )
            db.commit()
            log.warning(
                "created the local account '%s' with the %s password; it must be changed at first login",
                username,
                "AUTH_BOOTSTRAP_PASSWORD" if s.auth_bootstrap_password.strip() else "default",
            )
    finally:
        db.close()


def _render(request: Request, name: str, ctx: dict, status_code: int = 200):
    from .views import render

    return render(request, name, ctx, status_code=status_code)


def _login_context(s: Settings, next_path: str, error: str = "", username: str = "") -> dict:
    return {"user": None, "entra": s.auth_configured, "local": s.auth_local_enabled, "next": next_path, "error": error, "username": username, "hide_nav": True}


@router.get("/login")
def login(request: Request, next: str = "/", s: Settings = Depends(get_settings)):
    nxt = _safe_next(next)
    if not s.any_login:
        return _render(
            request, "login.html", _login_context(s, nxt, "No sign-in method is configured. Set AUTH_LOCAL_ENABLED=true or configure Entra ID."), 503
        )
    if s.auth_configured and not s.auth_local_enabled:
        return RedirectResponse(f"/auth/microsoft?next={nxt}", status_code=302)
    return _render(request, "login.html", _login_context(s, nxt))


@router.post("/local")
def local_login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
    s: Settings = Depends(get_settings),
):
    nxt = _safe_next(next)
    if not s.auth_local_enabled:
        raise HTTPException(status_code=404)
    from .services import log_activity

    uname = username.strip().lower()[:64]
    user = db.execute(select(User).where(User.username == uname, User.auth_provider == "local")).scalar_one_or_none()
    now = utcnow()
    generic = "Unknown username or wrong password."
    if user is None:
        verify_password(password, hash_password("dummy-timing-equaliser"))  # keep timing similar
        log_activity(db, uname, "login_failed", "user", "", {"reason": "unknown user"})
        return _render(request, "login.html", _login_context(s, nxt, generic, uname), 401)
    if user.disabled:
        log_activity(db, uname, "login_failed", "user", str(user.id), {"reason": "disabled"})
        return _render(request, "login.html", _login_context(s, nxt, generic, uname), 401)
    if user.locked_until and user.locked_until > now:
        minutes = int((user.locked_until - now).total_seconds() // 60) + 1
        log_activity(db, uname, "login_failed", "user", str(user.id), {"reason": "locked"})
        return _render(request, "login.html", _login_context(s, nxt, f"Account locked after repeated failures. Try again in {minutes} minutes.", uname), 423)
    if not verify_password(password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= s.login_max_failures:
            user.locked_until = now + timedelta(minutes=s.login_lockout_minutes)
            user.failed_logins = 0
        db.commit()
        log_activity(db, uname, "login_failed", "user", str(user.id), {"reason": "wrong password"})
        return _render(request, "login.html", _login_context(s, nxt, generic, uname), 401)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    db.commit()
    log_activity(db, user.username or "", "login", "user", str(user.id), {"provider": "local", "role": user.role})
    token = create_session(db, s, user, request)
    target = f"/auth/password?next={nxt}" if user.must_change_password else nxt
    resp = RedirectResponse(target, status_code=303)
    set_cookie(resp, s, token)
    return resp


@router.get("/password")
def password_form(request: Request, next: str = "/", user: User | None = Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    if user.auth_provider != "local":
        raise HTTPException(status_code=403, detail="Passwords of Microsoft accounts are managed in Entra ID")
    return _render(request, "password.html", {"next": _safe_next(next), "forced": user.must_change_password, "error": ""})


@router.post("/password")
def password_change(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    confirm: str = Form(""),
    next: str = Form("/"),
    user: User | None = Depends(current_user),
    db: Session = Depends(get_db),
    s: Settings = Depends(get_settings),
):
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    if user.auth_provider != "local":
        raise HTTPException(status_code=403, detail="Passwords of Microsoft accounts are managed in Entra ID")
    from .services import log_activity

    nxt = _safe_next(next)
    ctx = {"next": nxt, "forced": user.must_change_password, "error": ""}
    if not verify_password(current, user.password_hash):
        ctx["error"] = "The current password is wrong."
        return _render(request, "password.html", ctx, 400)
    if new != confirm:
        ctx["error"] = "The new passwords do not match."
        return _render(request, "password.html", ctx, 400)
    problems = password_problems(new, user.username or "")
    if verify_password(new, user.password_hash):
        problems.append("Choose a password different from the current one.")
    if problems:
        ctx["error"] = " ".join(problems)
        return _render(request, "password.html", ctx, 400)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    db.commit()
    # all other sessions of this account end; the current one is replaced
    end_all_sessions(db, user.id)
    log_activity(db, user.username or "", "password_change", "user", str(user.id), {})
    token = create_session(db, s, user, request)
    resp = RedirectResponse(nxt, status_code=303)
    set_cookie(resp, s, token)
    return resp


# --------------------------------------------------------------------------- user administration
@router.get("/users")
def users_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    users = db.execute(select(User).order_by(User.auth_provider, User.username, User.email)).scalars().all()
    return _render(request, "users.html", {"active": "users", "users": users, "local": s.auth_local_enabled, "temp": None, "temp_user": None})


@router.post("/users")
def users_create(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    role: str = Form("auditor"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    s: Settings = Depends(get_settings),
):
    from .services import log_activity
    from .views import redirect

    if not s.auth_local_enabled:
        return redirect("/auth/users", err="Local accounts are disabled (AUTH_LOCAL_ENABLED).")
    uname = username.strip().lower()[:64]
    if not uname.replace(".", "").replace("-", "").replace("_", "").isalnum():
        return redirect("/auth/users", err="Usernames may contain letters, digits, dots, dashes and underscores.")
    if db.execute(select(User).where(User.username == uname)).first():
        return redirect("/auth/users", err="That username exists already.")
    temp = temporary_password()
    new = User(
        oid=f"local:{uname}",
        email="",
        display_name=display_name.strip()[:200] or uname,
        role=role if role in ROLES else "auditor",
        auth_provider="local",
        username=uname,
        password_hash=hash_password(temp),
        must_change_password=True,
    )
    db.add(new)
    db.commit()
    log_activity(db, user.label, "user_create", "user", str(new.id), {"username": uname, "role": new.role})
    users = db.execute(select(User).order_by(User.auth_provider, User.username, User.email)).scalars().all()
    return _render(request, "users.html", {"active": "users", "users": users, "local": True, "temp": temp, "temp_user": new})


@router.post("/users/{user_id}/reset")
def users_reset(request: Request, user_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    from .services import log_activity
    from .views import redirect

    target = db.get(User, user_id)
    if target is None or target.auth_provider != "local":
        return redirect("/auth/users", err="Only local accounts have passwords here.")
    temp = temporary_password()
    target.password_hash = hash_password(temp)
    target.must_change_password = True
    target.failed_logins = 0
    target.locked_until = None
    db.commit()
    end_all_sessions(db, target.id)
    log_activity(db, user.label, "user_password_reset", "user", str(target.id), {"username": target.username})
    users = db.execute(select(User).order_by(User.auth_provider, User.username, User.email)).scalars().all()
    return _render(request, "users.html", {"active": "users", "users": users, "local": True, "temp": temp, "temp_user": target})


@router.post("/users/{user_id}/update")
def users_update(
    request: Request,
    user_id: int,
    role: str = Form(""),
    disabled: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from .services import log_activity
    from .views import redirect

    target = db.get(User, user_id)
    if target is None:
        return redirect("/auth/users", err="User not found.")
    if target.id == user.id:
        return redirect("/auth/users", err="You cannot change your own role or disable yourself.")
    if role in ROLES:
        target.role = role
    target.disabled = disabled == "1"
    db.commit()
    if target.disabled:
        end_all_sessions(db, target.id)
    log_activity(db, user.label, "user_update", "user", str(target.id), {"role": target.role, "disabled": target.disabled})
    return redirect("/auth/users", msg="User updated.")


@router.post("/users/{user_id}/delete")
def users_delete(request: Request, user_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    from .services import log_activity
    from .views import redirect

    target = db.get(User, user_id)
    if target is None:
        return redirect("/auth/users", err="User not found.")
    if target.id == user.id:
        return redirect("/auth/users", err="You cannot delete yourself.")
    end_all_sessions(db, target.id)
    label = target.username or target.email
    db.delete(target)
    db.commit()
    log_activity(db, user.label, "user_delete", "user", str(user_id), {"user": label})
    return redirect("/auth/users", msg="User deleted.")


# --------------------------------------------------------------------------- Entra routes
@router.get("/microsoft")
def microsoft(request: Request, next: str = "/", db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    if not s.auth_configured:
        raise HTTPException(status_code=503, detail="Single sign-on is not configured. Set AUTH_TENANT_ID, AUTH_CLIENT_ID and AUTH_CLIENT_SECRET.")
    db.execute(delete(LoginState).where(LoginState.expires_at < utcnow()))
    state = new_token(24)
    nonce = new_token(24)
    verifier, challenge = _pkce_pair()
    db.add(LoginState(state=state, nonce=nonce, code_verifier=verifier, next_path=_safe_next(next), expires_at=utcnow() + timedelta(minutes=10)))
    db.commit()
    params = {
        "client_id": s.auth_client_id,
        "response_type": "code",
        "redirect_uri": f"{s.app_base_url}/auth/callback",
        "response_mode": "query",
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(discovery(s)["authorization_endpoint"] + "?" + urlencode(params), status_code=302)


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
    s: Settings = Depends(get_settings),
):
    if error:
        log.warning("Entra returned error %s: %s", error, error_description)
        raise HTTPException(status_code=400, detail=f"Sign-in failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")
    ls = db.get(LoginState, state)
    if ls is not None:
        db.delete(ls)
        db.commit()
    if ls is None or ls.expires_at < utcnow():
        raise HTTPException(status_code=400, detail="Login state expired or unknown; start again")

    token_resp = httpx.post(
        discovery(s)["token_endpoint"],
        data={
            "client_id": s.auth_client_id,
            "client_secret": s.auth_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{s.app_base_url}/auth/callback",
            "code_verifier": ls.code_verifier,
            "scope": "openid profile email",
        },
        timeout=15,
    )
    if token_resp.status_code != 200:
        log.warning("token endpoint returned %s", token_resp.status_code)
        raise HTTPException(status_code=400, detail="Token exchange failed")
    id_token = token_resp.json().get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="No ID token returned")
    try:
        claims = validate_id_token(id_token, s, ls.nonce)
    except (JoseError, ValueError) as exc:
        log.warning("ID token validation failed: %s", exc)
        raise HTTPException(status_code=400, detail="ID token validation failed") from exc

    role = role_from_claims(claims, s)
    oid = claims.get("oid") or claims.get("sub")
    email = (claims.get("preferred_username") or claims.get("email") or "").lower()
    name = claims.get("name") or email
    if role is None:
        log.warning("user %s has no app role; access denied", email)
        return _render(
            request,
            "login.html",
            _login_context(s, ls.next_path, "Your Microsoft account is authenticated but has no Admin or Auditor app role for this application."),
            403,
        )
    user = db.execute(select(User).where(User.oid == oid)).scalar_one_or_none()
    if user is None:
        user = User(oid=oid, email=email, display_name=name, role=role, auth_provider="entra")
        db.add(user)
    else:
        user.email, user.display_name, user.role = email, name, role
    if user.disabled:
        return _render(request, "login.html", _login_context(s, ls.next_path, "This account is disabled."), 403)
    user.last_login_at = utcnow()
    db.commit()
    from .services import log_activity

    log_activity(db, email, "login", "user", str(user.id), {"provider": "entra", "role": role})
    token = create_session(db, s, user, request)
    resp = RedirectResponse(ls.next_path, status_code=302)
    set_cookie(resp, s, token)
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    token = request.cookies.get(COOKIE)
    if token:
        row = db.get(SessionRow, _hash(token))
        if row:
            db.delete(row)
            db.commit()
    resp = RedirectResponse("/auth/signed-out", status_code=303)
    clear_cookie(resp, s)
    return resp


@router.get("/signed-out")
def signed_out(request: Request):
    return _render(request, "signed_out.html", {"user": None})
