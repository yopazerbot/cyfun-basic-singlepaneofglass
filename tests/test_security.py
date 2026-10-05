"""Security controls: ID token validation, signed flash messages, request limits, upload limits,
account enumeration resistance, cookie hardening."""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, OctKey, RSAKey

from cyfun.auth import GENERIC_LOGIN_ERROR, SECURE_COOKIE, cookie_name, validate_id_token
from cyfun.config import Settings
from cyfun.export_xlsx import MAX_TOTAL_BYTES, ExportError, Package
from cyfun.views import _flash_sig

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def settings(**kw) -> Settings:
    base = {"APP_BASE_URL": "https://cyfun.example", "AUTH_TENANT_ID": TENANT, "AUTH_CLIENT_ID": CLIENT, "AUTH_CLIENT_SECRET": "x"}
    base.update(kw)
    return Settings(_env_file=None, **base)


# --------------------------------------------------------------------------- ID tokens
RSA = RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)
OTHER = RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)


def keys(_s, force=False):
    return KeySet([RSA])


def claims(**over):
    now = int(time.time())
    c = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": CLIENT,
        "tid": TENANT,
        "oid": "user-oid",
        "nonce": "n-123",
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "preferred_username": "someone@example.test",
        "roles": ["Admin"],
    }
    c.update(over)
    return c


def sign(c, key=RSA, alg="RS256"):
    return jwt.encode({"alg": alg, "kid": "k1"}, c, key)


def test_valid_token_accepted():
    out = validate_id_token(sign(claims()), settings(), "n-123", keys=keys)
    assert out["oid"] == "user-oid" and out["roles"] == ["Admin"]


@pytest.mark.parametrize(
    "bad",
    [
        {"iss": "https://login.microsoftonline.com/other-tenant/v2.0"},
        {"aud": "another-client"},
        {"tid": "other-tenant"},
        {"nonce": "replayed"},
        {"exp": int(time.time()) - 3600},
        {"nbf": int(time.time()) + 3600},
    ],
)
def test_wrong_claims_rejected(bad):
    with pytest.raises((JoseError, ValueError)):
        validate_id_token(sign(claims(**bad)), settings(), "n-123", keys=keys)


def test_missing_essential_claims_rejected():
    c = claims()
    del c["tid"]
    with pytest.raises((JoseError, ValueError)):
        validate_id_token(sign(c), settings(), "n-123", keys=keys)


def test_signature_from_another_key_rejected():
    with pytest.raises((JoseError, ValueError)):
        validate_id_token(sign(claims(), key=OTHER), settings(), "n-123", keys=keys)


def test_symmetric_and_unsigned_tokens_rejected():
    hs = jwt.encode({"alg": "HS256", "kid": "k1"}, claims(), OctKey.import_key("a-shared-secret-of-sufficient-length-1234"))
    with pytest.raises((JoseError, ValueError)):
        validate_id_token(hs, settings(), "n-123", keys=keys)
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    unsigned = f"{b64({'alg': 'none'})}.{b64(claims())}."
    with pytest.raises((JoseError, ValueError)):
        validate_id_token(unsigned, settings(), "n-123", keys=keys)


def test_tampered_payload_rejected():
    token = sign(claims())
    head, body, sig = token.split(".")
    import base64
    import json

    data = json.loads(base64.urlsafe_b64decode(body + "=="))
    data["roles"] = ["Admin", "Auditor", "Owner"]
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    with pytest.raises((JoseError, ValueError)):
        validate_id_token(f"{head}.{forged}.{sig}", settings(), "n-123", keys=keys)


# --------------------------------------------------------------------------- cookies
def test_host_prefixed_cookie_on_https():
    assert cookie_name(settings()) == SECURE_COOKIE
    assert cookie_name(settings(APP_BASE_URL="http://localhost:8000")) == "cyfun_session"


# --------------------------------------------------------------------------- flash messages
def test_unsigned_flash_messages_are_not_shown(admin):
    r = admin.get("/?err=Your+account+is+compromised.+Call+%2B32+000+00+00")
    assert "compromised" not in r.text
    good = admin.get(f"/?msg=Saved.&s={_flash_sig('msg', 'Saved.')}")
    assert 'class="flash ok"' in good.text
    forged = admin.get(f"/?msg=Other+text&s={_flash_sig('msg', 'Saved.')}")
    assert "Other text" not in forged.text


# --------------------------------------------------------------------------- request and upload limits
def test_oversized_request_refused(admin):
    r = admin.post("/evidence", content=b"x", headers={"Content-Length": str(500 * 1024 * 1024), "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_zip_bomb_refused_before_decompression():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml", b"0" * (MAX_TOTAL_BYTES + 1024))
    data = buf.getvalue()
    assert len(data) < 1024 * 1024  # small upload, huge content
    with pytest.raises(ExportError, match="far larger"):
        Package(data)


# --------------------------------------------------------------------------- enumeration
def test_login_failures_look_identical(app):
    with TestClient(app, base_url="http://testserver") as c:
        c.headers["Origin"] = "http://testserver"
        unknown = c.post("/auth/local", data={"username": "nobody-here", "password": "x"}, follow_redirects=False)
        known = c.post("/auth/local", data={"username": "admin", "password": "definitely-wrong"}, follow_redirects=False)
        assert unknown.status_code == known.status_code == 401
        assert GENERIC_LOGIN_ERROR in unknown.text and GENERIC_LOGIN_ERROR in known.text


def test_security_headers_present(client):
    h = client.get("/auth/login").headers
    for name in ("content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy", "permissions-policy", "cross-origin-opener-policy"):
        assert name in h, name
    assert h["x-permitted-cross-domain-policies"] == "none"


def test_flash_message_goes_before_the_fragment():
    from urllib.parse import urlsplit

    from cyfun.views import redirect

    loc = redirect("/settings#claude", err="Test failed").headers["location"]
    parts = urlsplit(loc)
    assert parts.path == "/settings" and parts.fragment == "claude" and "err=Test%20failed" in parts.query and "s=" in parts.query
    assert redirect("/x?a=1#y", msg="ok").headers["location"].startswith("/x?a=1&msg=ok&s=")
