"""Server settings from environment variables (or a .env file). See .env.example.

Sign-in, sessions, paths and the key that protects stored secrets are environment-only.
Connector credentials, the connector and backup schedules and the Claude settings are managed on the
Settings page (app/cyfun/appsettings.py); the matching environment variables below are
optional overrides that win over the stored values.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application ---------------------------------------------------------
    app_base_url: str = Field("http://localhost:8000", alias="APP_BASE_URL")
    app_name: str = Field("CyFun", alias="APP_NAME")
    data_dir: Path = Field(Path("./data"), alias="DATA_DIR")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    max_upload_mb: int = Field(25, alias="MAX_UPLOAD_MB")
    # Largest backup file accepted for a restore upload (Caddy has its own limit for /backup/restore).
    max_restore_mb: int = Field(2048, alias="MAX_RESTORE_MB")
    # Key that encrypts the secrets stored on the Settings page (AES-256-GCM). At least 32 characters.
    # Without it the Settings page cannot store secrets. Keep a copy outside the data volume backup.
    secret_key: str = Field("", alias="CYFUN_SECRET_KEY")

    # --- Sign-in ---------------------------------------------------------------
    # Local username/password accounts. Enabled by default so the application is usable
    # before single sign-on exists; a default "admin" account with password "admin" is
    # created on first start and must change its password at first login.
    auth_local_enabled: bool = Field(True, alias="AUTH_LOCAL_ENABLED")
    # Optional: password of the first local admin instead of the default "admin". Still changed at first login.
    auth_bootstrap_password: str = Field("", alias="AUTH_BOOTSTRAP_PASSWORD")
    # Microsoft Entra ID single sign-on (all three required to enable it).
    auth_tenant_id: str = Field("", alias="AUTH_TENANT_ID")
    auth_client_id: str = Field("", alias="AUTH_CLIENT_ID")
    auth_client_secret: str = Field("", alias="AUTH_CLIENT_SECRET")
    # Role given to an authenticated Entra user who carries no app role claim.
    # Leave empty to deny such users (recommended once app roles are assigned).
    auth_default_role: str = Field("", alias="AUTH_DEFAULT_ROLE")
    session_idle_minutes: int = Field(480, alias="SESSION_IDLE_MINUTES")
    session_absolute_hours: int = Field(12, alias="SESSION_ABSOLUTE_HOURS")
    login_max_failures: int = Field(5, alias="LOGIN_MAX_FAILURES")
    login_lockout_minutes: int = Field(15, alias="LOGIN_LOCKOUT_MINUTES")
    # Requests per minute per client address on /auth/* before 429 is returned.
    auth_rate_per_minute: int = Field(60, alias="AUTH_RATE_LIMIT_PER_MINUTE")

    # --- Optional overrides of the Settings page (a non-empty value wins) ------
    ms_graph_tenant_id: str = Field("", alias="MS_GRAPH_TENANT_ID")
    ms_graph_client_id: str = Field("", alias="MS_GRAPH_CLIENT_ID")
    ms_graph_client_secret: str = Field("", alias="MS_GRAPH_CLIENT_SECRET")

    github_token: str = Field("", alias="GITHUB_TOKEN")
    github_org: str = Field("", alias="GITHUB_ORG")

    railway_token: str = Field("", alias="RAILWAY_TOKEN")

    cloudflare_api_token: str = Field("", alias="CLOUDFLARE_API_TOKEN")
    cloudflare_account_id: str = Field("", alias="CLOUDFLARE_ACCOUNT_ID")

    notion_token: str = Field("", alias="NOTION_TOKEN")
    notion_database: str = Field("", alias="NOTION_DATABASE")

    connector_sync_hours: int | None = Field(None, alias="CONNECTOR_SYNC_HOURS")

    backup_interval_hours: int | None = Field(None, alias="BACKUP_INTERVAL_HOURS")
    backup_keep: int | None = Field(None, alias="BACKUP_KEEP")
    backup_onedrive_user: str = Field("", alias="BACKUP_ONEDRIVE_USER")
    backup_onedrive_folder: str = Field("", alias="BACKUP_ONEDRIVE_FOLDER")
    backup_graph_tenant_id: str = Field("", alias="BACKUP_GRAPH_TENANT_ID")
    backup_graph_client_id: str = Field("", alias="BACKUP_GRAPH_CLIENT_ID")
    backup_graph_client_secret: str = Field("", alias="BACKUP_GRAPH_CLIENT_SECRET")

    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    ai_model: str = Field("", alias="AI_MODEL")
    ai_effort: str = Field("", alias="AI_EFFORT")
    ai_monthly_cap_usd: int | None = Field(None, alias="AI_MONTHLY_CAP_USD")
    ai_review_after_sync: bool | None = Field(None, alias="AI_REVIEW_AFTER_SYNC")

    scheduler_enabled: bool = Field(True, alias="SCHEDULER_ENABLED")

    @field_validator("connector_sync_hours", "backup_interval_hours", "backup_keep", "ai_monthly_cap_usd", "ai_review_after_sync", mode="before")
    @classmethod
    def _empty_is_unset(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("app_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def auth_configured(self) -> bool:
        """Entra ID single sign-on is configured."""
        return bool(self.auth_tenant_id and self.auth_client_id and self.auth_client_secret)

    @property
    def any_login(self) -> bool:
        return self.auth_configured or self.auth_local_enabled

    @property
    def secure_cookies(self) -> bool:
        return self.app_base_url.startswith("https://")

    @property
    def origin(self) -> str:
        u = urlsplit(self.app_base_url)
        return f"{u.scheme}://{u.netloc}"

    @property
    def secret_key_problem(self) -> str:
        """Why stored secrets cannot be used, or an empty string when the key is usable."""
        key = self.secret_key.strip()
        if not key:
            return "CYFUN_SECRET_KEY is not set on the server."
        if len(key) < 32:
            return "CYFUN_SECRET_KEY is shorter than 32 characters."
        return ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cyfun.sqlite3"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "connector_snapshots"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
