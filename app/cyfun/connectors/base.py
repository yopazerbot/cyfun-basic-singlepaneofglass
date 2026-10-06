"""Connector contract.

A connector talks to one external system with read-only credentials from the
Settings page (or their environment overrides) and returns two things:

* inventory items: facts for the asset inventory (ID.AM-01.1, ID.AM-02.1, ...)
* check results: automated observations mapped to CyFun requirements, each with a
  status (pass, fail, warn, info, error), a one-line summary and structured details

Checks are evidence, not scores. The maturity scores stay a human judgement; the
assessment page shows the latest checks next to each requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

PASS, FAIL, WARN, INFO, ERROR = "pass", "fail", "warn", "info", "error"
STATUSES = (PASS, FAIL, WARN, INFO, ERROR)


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


@dataclass(frozen=True)
class CheckSpec:
    """One check's identity and requirement mapping, shared by its results and its error."""

    id: str
    title: str
    requirement_ids: tuple[str, ...]

    def result(self, status: str, summary: str, details: dict | None = None) -> Check:
        return Check(self.id, self.title, status, summary, list(self.requirement_ids), details or {})

    def error(self, exc: Exception, requirement_ids: list[str] | None = None) -> Check:
        """Error result for a failed call; mapped to the first requirement unless told otherwise."""
        rid = requirement_ids or [self.requirement_ids[0]]
        return Check(self.id, self.title, ERROR, Connector.describe_error(exc)[:300], rid, {"error": str(exc)[:1000]})


@dataclass
class SyncResult:
    inventory: list[InventoryItem] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # snapshot saved to disk as evidence


class Connector:
    key: str = ""
    name: str = ""
    description: str = ""
    env_vars: tuple[str, ...] = ()
    docs: str = ""

    def __init__(self, config):
        self.config = config  # appsettings.Config: effective credential values

    def configured(self) -> bool:
        raise NotImplementedError

    def test(self) -> str:
        """Cheap read-only call that proves the credentials work. Returns a one-line result; raises on failure."""
        raise NotImplementedError

    def sync(self) -> SyncResult:
        raise NotImplementedError

    # helpers -----------------------------------------------------------------
    @staticmethod
    def client(headers: dict | None = None, base_url: str = "") -> httpx.Client:
        return httpx.Client(base_url=base_url, headers=headers or {}, timeout=30, follow_redirects=False)

    @staticmethod
    def get_json(c: httpx.Client, url: str, params: dict | None = None):
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def describe_error(exc: Exception) -> str:
        """One line about a failed call, without response bodies (they can echo credentials)."""
        if isinstance(exc, httpx.HTTPStatusError):
            msg = f"HTTP {exc.response.status_code} from {exc.request.url.host}"
            if exc.response.status_code == 401:
                msg += " (credentials rejected or access missing)"
            elif exc.response.status_code == 403:
                msg += " (permission or licence missing)"
            return msg
        if isinstance(exc, httpx.TimeoutException):
            return f"timeout connecting to {exc.request.url.host}" if exc.request else "timeout"
        if isinstance(exc, httpx.TransportError):
            return f"no connection ({type(exc).__name__})"
        return str(exc)[:300]
