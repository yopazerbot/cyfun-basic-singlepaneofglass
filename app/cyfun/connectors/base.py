"""Connector contract.

A connector talks to one external system with read-only credentials from the
environment and returns two things:

* inventory items: facts for the asset inventory (ID.AM-01.1, ID.AM-02.1, ...)
* check results: automated observations mapped to CyFun requirements, each with a
  status (pass, fail, warn, info, error), a one-line summary and structured details

Checks are evidence, not scores. The maturity scores stay a human judgement; the
assessment page shows the latest checks next to each requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

PASS, FAIL, WARN, INFO, ERROR = "pass", "fail", "warn", "info", "error"


@dataclass
class InventoryItem:
    kind: str  # hardware|software|service|data|network|cloud|identity
    name: str
    external_id: str
    description: str = ""
    location: str = ""
    attributes: dict = field(default_factory=dict)


@dataclass
class Check:
    id: str
    title: str
    status: str
    summary: str
    requirement_ids: list[str]
    details: dict = field(default_factory=dict)


@dataclass
class SyncResult:
    inventory: list[InventoryItem] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # snapshot saved to disk as evidence


class ConnectorError(Exception):
    pass


class Connector:
    key: str = ""
    name: str = ""
    description: str = ""
    env_vars: tuple[str, ...] = ()
    docs: str = ""

    def __init__(self, settings):
        self.settings = settings

    def configured(self) -> bool:
        raise NotImplementedError

    def sync(self) -> SyncResult:
        raise NotImplementedError

    # helpers -----------------------------------------------------------------
    @staticmethod
    def client(headers: dict | None = None, base_url: str = "") -> httpx.Client:
        return httpx.Client(base_url=base_url, headers=headers or {}, timeout=30, follow_redirects=False)

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def error_check(check_id: str, title: str, requirement_ids: list[str], exc: Exception) -> Check:
        msg = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            msg = f"HTTP {exc.response.status_code} from {exc.request.url.host}"
            if exc.response.status_code in (401, 403):
                msg += " (permission or licence missing)"
        return Check(check_id, title, ERROR, msg[:300], requirement_ids, {"error": str(exc)[:1000]})
