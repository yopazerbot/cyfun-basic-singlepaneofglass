"""Database models. Single organisation, single assurance level (BASIC)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Organisation(Base):
    __tablename__ = "organisation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    legal_entity: Mapped[str] = mapped_column(String(200), default="")
    enterprise_number: Mapped[str] = mapped_column(String(50), default="")
    contact: Mapped[str] = mapped_column(String(200), default="")
    scope_description: Mapped[str] = mapped_column(Text, default="")
    scope_exclusions: Mapped[str] = mapped_column(Text, default="")
    cab_name: Mapped[str] = mapped_column(String(200), default="")
    self_assessment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    journey: Mapped[dict] = mapped_column(JSON, default=dict)  # stage -> {status, date, note}
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    oid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    display_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20), default="auditor")  # admin | auditor
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginState(Base):
    """Short-lived OIDC state: state value, nonce, PKCE verifier, return path."""

    __tablename__ = "login_state"
    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    next_path: Mapped[str] = mapped_column(String(500), default="/")
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class SessionRow(Base):
    __tablename__ = "session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")


class Score(Base):
    """One row per requirement. Scores 1..5; not_applicable overrides both."""

    __tablename__ = "score"
    requirement_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    doc_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impl_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    not_applicable: Mapped[bool] = mapped_column(Boolean, default=False)
    justification: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(320), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Snapshot(Base):
    __tablename__ = "snapshot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    taken_by: Mapped[str] = mapped_column(String(320), default="")
    total_maturity: Mapped[float | None] = mapped_column(Float, nullable=True)
    passes: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskAssessment(Base):
    __tablename__ = "risk_assessment"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sector_id: Mapped[str] = mapped_column(String(50), default="")
    organisation_size: Mapped[int] = mapped_column(Integer, default=1)  # 1 small, 2 medium, 3 large
    matrix: Mapped[dict] = mapped_column(JSON, default=dict)  # {"rows":[{"impact":..,"probability":[..5]}]}
    rationale: Mapped[str] = mapped_column(Text, default="")
    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    level: Mapped[str] = mapped_column(String(20), default="")
    updated_by: Mapped[str] = mapped_column(String(320), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RiskItem(Base):
    """Simple risk register (ID.RA-01.1, ID.RA-05.1)."""

    __tablename__ = "risk_item"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    threat: Mapped[str] = mapped_column(Text, default="")
    vulnerability: Mapped[str] = mapped_column(Text, default="")
    assets: Mapped[str] = mapped_column(String(300), default="")
    likelihood: Mapped[int] = mapped_column(Integer, default=2)  # 1 low 2 medium 3 high
    impact: Mapped[int] = mapped_column(Integer, default=2)
    treatment: Mapped[str] = mapped_column(String(20), default="mitigate")  # accept|mitigate|transfer|avoid
    measures: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|treated|accepted|closed
    review_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def score(self) -> int:
        return self.likelihood * self.impact


class Asset(Base):
    __tablename__ = "asset"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # hardware|software|service|data|network|cloud|identity
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(200), default="")  # site, environment, provider, region
    classification: Mapped[str] = mapped_column(String(20), default="Internal")  # Public|Internal|Confidential|Restricted
    criticality: Mapped[str] = mapped_column(String(10), default="Medium")  # Low|Medium|High
    primary_asset: Mapped[bool] = mapped_column(Boolean, default=False)
    lifecycle: Mapped[str] = mapped_column(String(20), default="active")  # planned|active|retired
    source: Mapped[str] = mapped_column(String(30), default="manual", index=True)  # manual | connector key
    external_id: Mapped[str] = mapped_column(String(300), default="", index=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Document(Base):
    """Register of policies, procedures, plans and records (documentation maturity)."""

    __tablename__ = "document"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    doc_type: Mapped[str] = mapped_column(String(20), default="policy")  # policy|procedure|plan|register|record|other
    owner: Mapped[str] = mapped_column(String(200), default="")
    version: Mapped[str] = mapped_column(String(50), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|approved|retired
    approved_by: Mapped[str] = mapped_column(String(200), default="")
    approved_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_review: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_review: Mapped[date | None] = mapped_column(Date, nullable=True)
    link: Mapped[str] = mapped_column(String(1000), default="")
    requirement_ids: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20), default="file")  # file|link|automated
    description: Mapped[str] = mapped_column(Text, default="")
    file_name: Mapped[str] = mapped_column(String(300), default="")
    stored_name: Mapped[str] = mapped_column(String(100), default="")
    mime: Mapped[str] = mapped_column(String(100), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    requirement_ids: Mapped[list] = mapped_column(JSON, default=list)
    collected_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    collected_by: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Action(Base):
    """Remediation action linked to a requirement."""

    __tablename__ = "action"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    requirement_id: Mapped[str] = mapped_column(String(20), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(200), default="")
    priority: Mapped[str] = mapped_column(String(10), default="medium")  # high|medium|low
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open|in_progress|done|cancelled
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ConnectorRun(Base):
    __tablename__ = "connector_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector: Mapped[str] = mapped_column(String(30), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="running")  # running|ok|error
    triggered_by: Mapped[str] = mapped_column(String(320), default="scheduler")
    inventory_count: Mapped[int] = mapped_column(Integer, default=0)
    check_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    snapshot_file: Mapped[str] = mapped_column(String(200), default="")


class CheckResult(Base):
    __tablename__ = "check_result"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("connector_run.id", ondelete="CASCADE"), index=True)
    connector: Mapped[str] = mapped_column(String(30), index=True)
    check_id: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(10))  # pass|fail|warn|info|error
    summary: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    requirement_ids: Mapped[list] = mapped_column(JSON, default=list)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Activity(Base):
    """Append-only activity log. The application exposes no update or delete for it."""

    __tablename__ = "activity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[str] = mapped_column(String(60), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
