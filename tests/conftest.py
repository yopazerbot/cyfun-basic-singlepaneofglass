"""Test fixtures: isolated data directory, app without scheduler, authenticated clients.

Authentication in tests does not bypass anything in the application: a user row and a
session row are inserted directly, exactly as the OIDC callback would do, and the
opaque session token is set as the cookie.
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.update(
    {
        "DATA_DIR": str(Path(os.environ.get("PYTEST_DATA_DIR", ROOT / ".pytest-data"))),
        "SCHEDULER_ENABLED": "false",
        "APP_BASE_URL": "http://testserver",
        "AUTH_TENANT_ID": "",
        "AUTH_CLIENT_ID": "",
        "AUTH_CLIENT_SECRET": "",
        "LOG_LEVEL": "WARNING",
        "AUTH_RATE_LIMIT_PER_MINUTE": "100000",
        # Isolate the tests from a developer's .env and shell: no real credentials, a fixed key.
        "CYFUN_SECRET_KEY": "test-secret-key-0123456789abcdef-0123456789",
        "ANTHROPIC_API_KEY": "",
        "AI_MODEL": "",
        "AI_EFFORT": "",
        "AI_MONTHLY_CAP_USD": "",
        "AI_REVIEW_AFTER_SYNC": "",
        "CONNECTOR_SYNC_HOURS": "",
        "MS_GRAPH_TENANT_ID": "",
        "MS_GRAPH_CLIENT_ID": "",
        "MS_GRAPH_CLIENT_SECRET": "",
        "GITHUB_TOKEN": "",
        "GITHUB_ORG": "",
        "RAILWAY_TOKEN": "",
        "CLOUDFLARE_API_TOKEN": "",
        "CLOUDFLARE_ACCOUNT_ID": "",
    }
)

from cyfun.config import get_settings  # noqa: E402

get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from cyfun import db as database  # noqa: E402
from cyfun.auth import COOKIE  # noqa: E402
from cyfun.config import new_token  # noqa: E402
from cyfun.main import create_app  # noqa: E402
from cyfun.models import SessionRow, User, utcnow  # noqa: E402


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("data")
    os.environ["DATA_DIR"] = str(d)
    get_settings.cache_clear()
    return d


@pytest.fixture(scope="session")
def app(data_dir):
    return create_app(get_settings())


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _login(client: TestClient, role: str, email: str) -> TestClient:
    db = database.session()
    user = db.query(User).filter(User.oid == f"oid-{role}").one_or_none()
    if user is None:
        user = User(oid=f"oid-{role}", email=email, display_name=email.split("@")[0], role=role)
        db.add(user)
        db.commit()
    token = new_token(32)
    now = utcnow()
    db.add(
        SessionRow(
            token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, created_at=now, last_seen_at=now, expires_at=now + timedelta(hours=12)
        )
    )
    db.commit()
    db.close()
    client.cookies.set(COOKIE, token)
    client.headers["Origin"] = "http://testserver"
    return client


@pytest.fixture
def admin(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield _login(c, "admin", "admin@example.test")


@pytest.fixture
def auditor(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield _login(c, "auditor", "auditor@example.test")
