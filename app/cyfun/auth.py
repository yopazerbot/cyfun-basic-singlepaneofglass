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
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import db as database
from .config import Settings, get_settings, new_token
from .db import get_db
from .models import LoginState, SessionRow, User, utcnow
from .passwords import hash_password, password_problems, temporary_password, verify_password
from .services import log_activity
from .views import redirect, render

log = logging.getLogger("cyfun.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = "cyfun_session"
SECURE_COOKIE = "__Host-cyfun_session"  # https: host-only, Secure, path=/ enforced by the browser
ID_TOKEN_ALGORITHMS = ["RS256"]  # Entra ID signs ID tokens with RS256; anything else is refused
OIDC_SCOPE = "openid profile email"
GENERIC_LOGIN_ERROR = "Unknown username or wrong password. After repeated failures the account is locked for a few minutes."
ROLES = ("admin", "auditor")  # in order of precedence
DEFAULT_ADMIN = ("admin", "admin")

_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, KeySet]] = {}


_DUMMY_HASH = hash_password(new_token(16))  # verified for unknown users so every attempt costs the same


class PasswordChangeRequired(Exception):  # noqa: N818 - control-flow signal handled by the app
    pass


# --------------------------------------------------------------------------- Entra helpers
def _authority(s: Settings) -> str:
    return f"https://login.microsoftonline.com/{s.auth_tenant_id}/v2.0"


def _redirect_uri(s: Settings) -> str:
    return f"{s.app_base_url}/auth/callback"


def _cached_fetch(cache: dict, s: Settings, url, parse, force: bool = False):
    """GET a JSON document per tenant and keep the parsed result for an hour. `url` is called only on a miss."""
    now = time.time()
    cached = cache.get(s.auth_tenant_id)
    if cached and cached[0] > now and not force:
        return cached[1]
    r = httpx.get(url(), timeout=10)
    r.raise_for_status()
    value = parse(r.json())
    cache[s.auth_tenant_id] = (now + 3600, value)
    return value


def discovery(s: Settings) -> dict:
    return _cached_fetch(_discovery_cache, s, lambda: f"{_authority(s)}/.well-known/openid-configuration", dict)


def jwks(s: Settings, force: bool = False) -> KeySet:
    return _cached_fetch(_jwks_cache, s, lambda: discovery(s)["jwks_uri"], KeySet.import_key_set, force)


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
    found = {r.lower() for r in roles if isinstance(r, str)}
    for role in ROLES:
        if role in found:
            return role
    default = s.auth_default_role.strip().lower()
    return default if default in ROLES else None


def validate_id_token(id_token: str, s: Settings, nonce: str, keys=None) -> dict:
    """Verify signature (RS256 only, key from the tenant JWKS) and claims: iss, aud, exp, nbf, iat, nonce, tid.

    The JWKS is fetched again once when the signature cannot be verified (key rotation)."""
    load = keys or jwks
    try:
        token = jwt.decode(id_token, load(s), algorithms=ID_TOKEN_ALGORITHMS)
    except (JoseError, ValueError):
        token = jwt.decode(id_token, load(s, force=True), algorithms=ID_TOKEN_ALGORITHMS)
    registry = jwt.JWTClaimsRegistry(
        leeway=60,
        iss={"essential": True, "value": _authority(s)},
        aud={"essential": True, "value": s.auth_client_id},
        exp={"essential": True},
        nonce={"essential": True, "value": nonce},
        tid={"essential": True, "value": s.auth_tenant_id},
    )
    registry.validate(token.claims)
    return dict(token.claims)


# --------------------------------------------------------------------------- sessions
def cookie_name(s: Settings) -> str:
    return SECURE_COOKIE if s.secure_cookies else COOKIE


def create_session(db: Session, s: Settings, user: User, request: Request) -> str:
    token = new_token(32)
    now = utcnow()
    db.execute(delete(SessionRow).where(SessionRow.expires_at < now))  # housekeeping
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
    response.set_cookie(cookie_name(s), token, max_age=s.session_absolute_hours * 3600, httponly=True, secure=s.secure_cookies, samesite="lax", path="/")


def clear_cookie(response: Response, s: Settings) -> None:
    response.delete_cookie(cookie_name(s), path="/", httponly=True, secure=s.secure_cookies, samesite="lax")


def _signed_in(db: Session, s: Settings, user: User, request: Request, target: str, status_code: int) -> RedirectResponse:
    """Start a session for `user` and redirect to `target` with the session cookie."""
    token = create_session(db, s, user, request)
    resp = RedirectResponse(target, status_code=status_code)
    set_cookie(resp, s, token)
    return resp


def load_user(request: Request, db: Session, s: Settings) -> User | None:
    token = request.cookies.get(cookie_name(s))
    if not token:
        return None
    row = db.get(SessionRow, _hash(token))
    if row is None:
        return None
    now = utcnow()
    expired = now > row.expires_at or now > row.last_seen_at + timedelta(minutes=s.session_idle_minutes)
    user = None if expired else db.get(User, row.user_id)
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
def _new_local_user(username: str, display_name: str, role: str, password: str) -> User:
    """A local account that must change its password at first login."""
    return User(
        oid=f"local:{username}",
        email="",
        display_name=display_name,
        role=role,
        auth_provider="local",
        username=username,
        password_hash=hash_password(password),
        must_change_password=True,
    )


def _record_failed_password(user: User, s: Settings, now: datetime) -> bool:
    """Count a wrong password. At the limit the account locks and the count restarts; returns True then."""
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins < s.login_max_failures:
        return False
    user.locked_until = now + timedelta(minutes=s.login_lockout_minutes)
    user.failed_logins = 0
    return True


def ensure_default_admin(s: Settings) -> None:
    """Create the first administrator (password change forced) when local login is on and no user exists at all.

    A deployment that already has users (for example Entra accounts) never gets a default account
    added later; an Entra administrator creates local accounts on the Users page instead."""
    if not s.auth_local_enabled:
        return
    with database.session() as db:
        if db.execute(select(User.id).limit(1)).first() is not None:
            return
        username, password = DEFAULT_ADMIN
        bootstrap = s.auth_bootstrap_password.strip()
        db.add(_new_local_user(username, "Administrator", "admin", bootstrap or password))
        db.commit()
        log.warning(
            "created the local account '%s' with the %s password; it must be changed at first login",
            username,
            "AUTH_BOOTSTRAP_PASSWORD" if bootstrap else "default",
        )


def _login_page(request: Request, s: Settings, next_path: str, error: str = "", username: str = "", status_code: int = 200):
    ctx = {"user": None, "entra": s.auth_configured, "local": s.auth_local_enabled, "next": next_path, "error": error, "username": username, "hide_nav": True}
    return render(request, "login.html", ctx, status_code=status_code)


def _password_page(request: Request, user: User, next_path: str, error: str = "", status_code: int = 200):
    return render(request, "password.html", {"next": next_path, "forced": user.must_change_password, "error": error}, status_code=status_code)


def _local_account(user: User | None) -> User:
    """The signed-in user, who must have a local account to manage a password here."""
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    if user.auth_provider != "local":
        raise HTTPException(status_code=403, detail="Passwords of Microsoft accounts are managed in Entra ID")
    return user


@router.get("/login")
def login(request: Request, next: str = "/", s: Settings = Depends(get_settings)):
    nxt = _safe_next(next)
    if not s.any_login:
        return _login_page(request, s, nxt, "No sign-in method is configured. Set AUTH_LOCAL_ENABLED=true or configure Entra ID.", status_code=503)
    if s.auth_configured and not s.auth_local_enabled:
        return RedirectResponse(f"/auth/microsoft?next={nxt}", status_code=302)
    return _login_page(request, s, nxt)


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
    uname = username.strip().lower()[:64]
    user = db.execute(select(User).where(User.username == uname, User.auth_provider == "local")).scalar_one_or_none()
    now = utcnow()
    # Every failure gets the same message and status, and exactly one password verification runs
    # in every branch, so neither the response nor its timing reveals whether an account exists.
    password_ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if user is None:
        refusal = "unknown user"
    elif user.disabled:
        refusal = "disabled"
    elif user.locked_until and user.locked_until > now:
        refusal = "locked"
    elif not password_ok:
        refusal = "wrong password"
        _record_failed_password(user, s, now)
        db.commit()
    else:
        refusal = ""
    if refusal:
        log_activity(db, uname, "login_failed", "user", str(user.id) if user else "", {"reason": refusal})
        return _login_page(request, s, nxt, GENERIC_LOGIN_ERROR, uname, 401)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    db.commit()
    log_activity(db, user.username or "", "login", "user", str(user.id), {"provider": "local", "role": user.role})
    return _signed_in(db, s, user, request, f"/auth/password?next={nxt}" if user.must_change_password else nxt, 303)


@router.get("/password")
def password_form(request: Request, next: str = "/", user: User | None = Depends(current_user)):
    return _password_page(request, _local_account(user), _safe_next(next))


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
    user = _local_account(user)
    nxt = _safe_next(next)
    if not verify_password(current, user.password_hash):
        locked = _record_failed_password(user, s, utcnow())
        db.commit()
        if locked:
            end_all_sessions(db, user.id)
            log_activity(db, user.label, "password_change_locked", "user", str(user.id), {})
            resp = RedirectResponse("/auth/login", status_code=303)
            clear_cookie(resp, s)
            return resp
        log_activity(db, user.label, "password_change_failed", "user", str(user.id), {})
        return _password_page(request, user, nxt, "The current password is wrong.", 400)
    if new != confirm:
        return _password_page(request, user, nxt, "The new passwords do not match.", 400)
    problems = password_problems(new, user.username or "")
    if verify_password(new, user.password_hash):
        problems.append("Choose a password different from the current one.")
    if problems:
        return _password_page(request, user, nxt, " ".join(problems), 400)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.failed_logins = 0
    db.commit()
    # all other sessions of this account end; the current one is replaced
    end_all_sessions(db, user.id)
    log_activity(db, user.username or "", "password_change", "user", str(user.id), {})
    return _signed_in(db, s, user, request, nxt, 303)


# --------------------------------------------------------------------------- user administration
def _users_page(request: Request, db: Session, local: bool, temp: str | None = None, temp_user: User | None = None):
    users = db.execute(select(User).order_by(User.auth_provider, User.username, User.email)).scalars().all()
    return render(request, "users.html", {"active": "users", "users": users, "local": local, "temp": temp, "temp_user": temp_user})


@router.get("/users")
def users_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    return _users_page(request, db, s.auth_local_enabled)


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
    if not s.auth_local_enabled:
        return redirect("/auth/users", err="Local accounts are disabled (AUTH_LOCAL_ENABLED).")
    uname = username.strip().lower()[:64]
    if not uname.replace(".", "").replace("-", "").replace("_", "").isalnum():
        return redirect("/auth/users", err="Usernames may contain letters, digits, dots, dashes and underscores.")
    if db.execute(select(User).where(User.username == uname)).first():
        return redirect("/auth/users", err="That username exists already.")
    temp = temporary_password()
    new = _new_local_user(uname, display_name.strip()[:200] or uname, role if role in ROLES else "auditor", temp)
    db.add(new)
    db.commit()
    log_activity(db, user.label, "user_create", "user", str(new.id), {"username": uname, "role": new.role})
    return _users_page(request, db, True, temp, new)


@router.post("/users/{user_id}/reset")
def users_reset(request: Request, user_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
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
    return _users_page(request, db, True, temp, target)


@router.post("/users/{user_id}/update")
def users_update(
    request: Request,
    user_id: int,
    role: str = Form(""),
    disabled: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
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
        "redirect_uri": _redirect_uri(s),
        "response_mode": "query",
        "scope": OIDC_SCOPE,
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
            "redirect_uri": _redirect_uri(s),
            "code_verifier": ls.code_verifier,
            "scope": OIDC_SCOPE,
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
        return _login_page(
            request, s, ls.next_path, "Your Microsoft account is authenticated but has no Admin or Auditor app role for this application.", status_code=403
        )
    user = db.execute(select(User).where(User.oid == oid)).scalar_one_or_none()
    if user is None:
        user = User(oid=oid, email=email, display_name=name, role=role, auth_provider="entra")
        db.add(user)
    else:
        user.email, user.display_name, user.role = email, name, role
    if user.disabled:
        return _login_page(request, s, ls.next_path, "This account is disabled.", status_code=403)
    user.last_login_at = utcnow()
    db.commit()
    log_activity(db, email, "login", "user", str(user.id), {"provider": "entra", "role": role})
    return _signed_in(db, s, user, request, ls.next_path, 302)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    token = request.cookies.get(cookie_name(s))
    row = db.get(SessionRow, _hash(token)) if token else None
    if row is not None:
        db.delete(row)
        db.commit()
    resp = RedirectResponse("/auth/signed-out", status_code=303)
    clear_cookie(resp, s)
    return resp


@router.get("/signed-out")
def signed_out(request: Request):
    return render(request, "signed_out.html", {"user": None})
