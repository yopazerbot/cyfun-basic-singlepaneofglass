"""Microsoft Entra ID single sign-on (OpenID Connect, authorization code flow with PKCE).

Flow
----
1. GET /auth/login stores state, nonce and a PKCE verifier in the database (10 minutes)
   and redirects to the tenant's authorization endpoint.
2. GET /auth/callback exchanges the code for an ID token (client secret + PKCE verifier),
   validates signature, issuer, audience, expiry, nonce and tenant, derives the role
   from the app roles claim, upserts the user and creates a server-side session.
3. The browser holds only an opaque random session token in an HttpOnly cookie. The
   database stores its SHA-256 hash. Sessions expire after an idle period and after an
   absolute lifetime.

Roles
-----
Entra app roles are mapped by value: "Admin" (full access) and "Auditor" (read-only).
A user without a role claim gets AUTH_DEFAULT_ROLE if set, otherwise access is denied.
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
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete
from sqlalchemy.orm import Session

from .config import Settings, get_settings, new_token
from .db import get_db
from .models import LoginState, SessionRow, User, utcnow

log = logging.getLogger("cyfun.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = "cyfun_session"
ROLES = ("admin", "auditor")
ROLE_MAP = {"admin": "admin", "auditor": "auditor"}

_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, object]] = {}


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


# --------------------------------------------------------------------------- session helpers
def create_session(db: Session, s: Settings, user: User, request: Request) -> str:
    token = new_token(32)
    now = utcnow()
    row = SessionRow(
        token_hash=_hash(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=s.session_absolute_hours),
        ip=(request.client.host if request.client else "")[:64],
        user_agent=(request.headers.get("user-agent") or "")[:300],
    )
    db.add(row)
    db.commit()
    return token


def set_cookie(response: Response, s: Settings, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=s.session_absolute_hours * 3600,
        httponly=True,
        secure=s.secure_cookies,
        samesite="lax",
        path="/",
    )


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
    idle_limit = row.last_seen_at + timedelta(minutes=s.session_idle_minutes)
    if now > row.expires_at or now > idle_limit:
        db.delete(row)
        db.commit()
        return None
    if (now - row.last_seen_at).total_seconds() > 60:
        row.last_seen_at = now
        db.commit()
    return db.get(User, row.user_id)


# --------------------------------------------------------------------------- dependencies
def current_user(request: Request, db: Session = Depends(get_db), s: Settings = Depends(get_settings)) -> User | None:
    user = load_user(request, db, s)
    request.state.user = user
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="This action needs the Admin role")
    return user


# --------------------------------------------------------------------------- routes
@router.get("/login")
def login(request: Request, next: str = "/", db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    if not s.auth_configured:
        return HTMLResponse(
            "<h1>Single sign-on is not configured</h1><p>Set AUTH_TENANT_ID, AUTH_CLIENT_ID and AUTH_CLIENT_SECRET. See docs/entra-id-sso.md.</p>",
            status_code=503,
        )
    db.execute(delete(LoginState).where(LoginState.expires_at < utcnow()))
    state = new_token(24)
    nonce = new_token(24)
    verifier, challenge = _pkce_pair()
    db.add(
        LoginState(
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            next_path=_safe_next(next),
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
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
    url = discovery(s)["authorization_endpoint"] + "?" + urlencode(params)
    return RedirectResponse(url, status_code=302)


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
        return HTMLResponse(
            "<h1>No role assigned</h1><p>Your account is authenticated but has no Admin or Auditor "
            "app role for this application. Ask the administrator to assign one in Microsoft Entra ID.</p>",
            status_code=403,
        )
    user = db.query(User).filter(User.oid == oid).one_or_none()
    if user is None:
        user = User(oid=oid, email=email, display_name=name, role=role)
        db.add(user)
    else:
        user.email, user.display_name, user.role = email, name, role
    user.last_login_at = utcnow()
    db.commit()
    from .services import log_activity

    log_activity(db, email, "login", "user", str(user.id), {"role": role})
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
    from .main import templates

    return templates.TemplateResponse(request, "signed_out.html", {"user": None})
