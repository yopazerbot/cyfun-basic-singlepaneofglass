"""Settings page: encrypted secrets, environment overrides, validation, access, connection tests."""

import re

import pytest

from cyfun import db as database
from cyfun import secretbox
from cyfun.appsettings import load_config, save_group
from cyfun.config import Settings, get_settings
from cyfun.connectors import registry
from cyfun.connectors.github import GitHubConnector
from cyfun.models import Activity, AppSetting

TOKEN = "test-github-token-0123456789abcdefWXYZ"


@pytest.fixture
def clean_settings(client):
    yield
    db = database.session()
    db.query(AppSetting).delete()
    db.commit()
    db.close()


def _settings(**env) -> Settings:
    base = {"CYFUN_SECRET_KEY": get_settings().secret_key, "DATA_DIR": str(get_settings().data_dir)}
    base.update(env)
    return Settings(_env_file=None, **base)


def test_secretbox_round_trip_and_binding():
    s = get_settings()
    token, kid = secretbox.encrypt(s, "github_token", "secret-value")
    assert "secret-value" not in token
    assert secretbox.decrypt(s, "github_token", token, kid) == "secret-value"
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt(s, "railway_token", token, kid)  # bound to the setting name
    other = _settings(CYFUN_SECRET_KEY="another-server-key-0123456789-abcdefghij")
    with pytest.raises(secretbox.SecretBoxError, match="different CYFUN_SECRET_KEY"):
        secretbox.decrypt(other, "github_token", token, kid)


def test_short_or_missing_key_is_reported():
    assert "not set" in _settings(CYFUN_SECRET_KEY="").secret_key_problem
    assert "shorter than 32" in _settings(CYFUN_SECRET_KEY="short").secret_key_problem
    assert get_settings().secret_key_problem == ""


def test_settings_page_is_admin_only(auditor, client):
    assert auditor.get("/settings").status_code == 403
    assert auditor.post("/settings/github", data={"github_token": TOKEN}).status_code == 403
    client.cookies.clear()
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/auth/login")


def test_secret_is_encrypted_never_shown_and_never_logged(admin, clean_settings):
    r = admin.post("/settings/github", data={"github_token": TOKEN, "github_org": "example-org"}, follow_redirects=False)
    assert r.status_code == 303 and "saved" in r.headers["location"]
    db = database.session()
    row = db.get(AppSetting, "github_token")
    assert row is not None and row.value == "" and TOKEN not in row.ciphertext and row.hint == "WXYZ"
    assert db.get(AppSetting, "github_org").value == "example-org"
    logged = db.query(Activity).filter(Activity.action == "settings_update").order_by(Activity.id.desc()).first()
    assert TOKEN not in str(logged.details) and "Token" in str(logged.details)
    db.close()
    page = admin.get("/settings").text
    assert TOKEN not in page
    assert "…WXYZ" in page
    cfg = load_config()
    assert cfg.github_token == TOKEN and cfg.sources["github_token"] == "app"
    assert registry(cfg)["github"].configured()
    # an empty field keeps the stored value; the clear box removes it
    admin.post("/settings/github", data={"github_token": "", "github_org": "example-org"})
    assert load_config().github_token == TOKEN
    admin.post("/settings/github", data={"github_token__clear": "1", "github_org": ""})
    cfg = load_config()
    assert cfg.github_token == "" and cfg.github_org == "" and not registry(cfg)["github"].configured()


def test_invalid_values_store_nothing(admin, clean_settings):
    r = admin.post("/settings/github", data={"github_token": TOKEN, "github_org": "bad org name!"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = admin.post("/settings/github", data={"github_token": "has space inside"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = admin.post("/settings/microsoft", data={"ms_graph_tenant_id": "not-a-guid"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = admin.post("/settings/schedule", data={"connector_sync_hours": "0"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    db = database.session()
    assert db.query(AppSetting).count() == 0
    db.close()


def test_schedule_and_claude_choices(admin, clean_settings):
    admin.post("/settings/schedule", data={"connector_sync_hours": "12"})
    admin.post("/settings/claude", data={"ai_model": "claude-sonnet-5-5", "ai_effort": "high", "ai_monthly_cap_usd": "40", "ai_review_after_sync": "1"})
    cfg = load_config()
    assert cfg.connector_sync_hours == 12
    assert (cfg.ai_model, cfg.ai_effort, cfg.ai_monthly_cap_usd, cfg.ai_review_after_sync) == ("claude-sonnet-5-5", "high", 40, True)
    r = admin.post("/settings/claude", data={"ai_model": "gpt-4"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    admin.post("/settings/claude", data={"ai_model": "claude-opus-5-5", "ai_effort": "medium", "ai_monthly_cap_usd": ""})
    cfg = load_config()
    assert cfg.ai_monthly_cap_usd == 25 and cfg.ai_review_after_sync is False  # unchecked box, empty number back to default


def test_environment_value_wins_and_is_read_only(clean_settings):
    s = _settings(GITHUB_TOKEN="env-token-value-0123456789", CONNECTOR_SYNC_HOURS="6")
    db = database.session()
    changes, errors = save_group(db, s, "github", {"github_token": TOKEN}, "tester")
    assert errors == [] and changes == []  # nothing to store: the environment owns the field
    cfg = load_config(db, s)
    assert cfg.github_token == "env-token-value-0123456789" and cfg.sources["github_token"] == "env"
    assert cfg.connector_sync_hours == 6 and cfg.sources["connector_sync_hours"] == "env"
    db.close()


def test_secret_cannot_be_stored_without_server_key(clean_settings):
    db = database.session()
    changes, errors = save_group(db, _settings(CYFUN_SECRET_KEY=""), "railway", {"railway_token": "abc123"}, "tester")
    assert changes == [] and "CYFUN_SECRET_KEY" in errors[0]
    db.close()


def test_stored_secret_under_another_key_is_flagged(admin, clean_settings):
    admin.post("/settings/railway", data={"railway_token": "railway-token-0123456789abcdef"})
    other = _settings(CYFUN_SECRET_KEY="rotated-server-key-0123456789-abcdefghijk")
    cfg = load_config(settings=other)
    assert cfg.railway_token == "" and cfg.sources["railway_token"] == "unreadable"
    assert "different CYFUN_SECRET_KEY" in cfg.problems["railway_token"]


def test_connection_test_uses_typed_values_without_saving(admin, clean_settings, monkeypatch):
    seen = {}

    def fake_test(self):
        seen["token"] = self.config.github_token
        return "Token works for tester (user mode)."

    monkeypatch.setattr(GitHubConnector, "test", fake_test)
    r = admin.post("/settings/github/test", data={"github_token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and "Token+works" in r.headers["location"].replace("%20", "+") and "not+saved" in r.headers["location"].replace("%20", "+")
    assert seen["token"] == TOKEN
    assert load_config().github_token == ""

    def failing(self):
        raise RuntimeError("bad credentials")

    monkeypatch.setattr(GitHubConnector, "test", failing)
    r = admin.post("/settings/github/test", data={"github_token": TOKEN}, follow_redirects=False)
    assert "err=" in r.headers["location"] and "bad%20credentials" in r.headers["location"]


def test_claude_key_test(admin, clean_settings, monkeypatch):
    from cyfun.ai import claude

    monkeypatch.setattr(claude, "test_key", lambda key, model: f"The key works and {model} is available.")
    r = admin.post("/settings/claude/test", data={"anthropic_api_key": "test-anthropic-key-0123456789abcdefghij"}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and "claude-opus-5-5" in r.headers["location"]
    r = admin.post("/settings/claude/test", data={}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_settings_page_renders_every_group(admin, clean_settings):
    page = admin.get("/settings").text
    for anchor in ("microsoft", "github", "railway", "cloudflare", "schedule", "claude"):
        assert re.search(rf'id="{anchor}"', page), anchor
    assert 'type="password"' in page and 'autocomplete="new-password"' in page
