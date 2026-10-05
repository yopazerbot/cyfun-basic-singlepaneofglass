from cyfun.auth import _safe_next, role_from_claims
from cyfun.config import Settings


def settings(**kw) -> Settings:
    base = {"APP_BASE_URL": "https://cyfun.example", "AUTH_TENANT_ID": "t", "AUTH_CLIENT_ID": "c", "AUTH_CLIENT_SECRET": "s"}
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_roles_from_app_roles_claim():
    s = settings()
    assert role_from_claims({"roles": ["Admin"]}, s) == "admin"
    assert role_from_claims({"roles": ["auditor"]}, s) == "auditor"
    assert role_from_claims({"roles": ["Auditor", "Admin"]}, s) == "admin"
    assert role_from_claims({"roles": ["Reader"]}, s) is None
    assert role_from_claims({}, s) is None


def test_default_role_only_when_configured():
    assert role_from_claims({}, settings(AUTH_DEFAULT_ROLE="admin")) == "admin"
    assert role_from_claims({}, settings(AUTH_DEFAULT_ROLE="superuser")) is None


def test_safe_next_blocks_open_redirects():
    assert _safe_next("/assessment/PR.AA-01.1") == "/assessment/PR.AA-01.1"
    assert _safe_next("//evil.example") == "/"
    assert _safe_next("https://evil.example") == "/"
    assert _safe_next("") == "/"
    assert _safe_next(None) == "/"


def test_settings_derivations():
    s = settings()
    assert s.auth_configured
    assert s.secure_cookies
    assert s.origin == "https://cyfun.example"
    assert not settings(APP_BASE_URL="http://localhost:8000").secure_cookies
