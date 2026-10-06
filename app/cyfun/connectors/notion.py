"""Notion connector: the document register comes from one Notion database.

Each page of the database is a policy, procedure, plan, register or record. A sync copies the
page metadata into the document register (source "notion", read-only in the application, edited
in Notion) and checks approval and review dates for GV.PO-01.1. Page content stays in Notion.

Integration: an internal integration (notion.so/profile/integrations) with "Read content"; also
"Insert content" when the application should create the database. Share the database (or its
parent page) with the integration. Notion API version 2025-09-03 (databases hold data sources).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import httpx

from .base import FAIL, INFO, PASS, WARN, CheckSpec, Connector, SyncResult

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
REQ_ID = re.compile(r"\b[A-Z]{2}\.[A-Z]{2}-\d{2}\.\d+\b")
NOTION_ID = re.compile(r"([0-9a-fA-F]{8})-?([0-9a-fA-F]{4})-?([0-9a-fA-F]{4})-?([0-9a-fA-F]{4})-?([0-9a-fA-F]{12})")
TYPES = ("policy", "procedure", "plan", "register", "record", "other")
REVIEW_YEARS = 2  # documentation maturity 3 and up needs a review within two years

REGISTER = CheckSpec("notion-documents", "Documented information in Notion", ("GV.PO-01.1",))
REVIEWS = CheckSpec("notion-reviews", "Document reviews up to date", ("GV.PO-01.1",))
MAPPING = CheckSpec("notion-mapping", "Documents linked to requirements", ("GV.PO-01.1",))

# The database the application creates; sync reads these names case-insensitively, other names are ignored.
SCHEMA = {
    "Name": {"title": {}},
    "Type": {"select": {"options": [{"name": t.capitalize()} for t in TYPES]}},
    "Status": {"select": {"options": [{"name": "Draft", "color": "gray"}, {"name": "Approved", "color": "green"}, {"name": "Retired", "color": "red"}]}},
    "Owner": {"people": {}},
    "Version": {"rich_text": {}},
    "Approved by": {"people": {}},
    "Approved on": {"date": {}},
    "Last review": {"date": {}},
    "Next review": {"date": {}},
    "Requirements": {"multi_select": {"options": []}},
}


def notion_id(value: str) -> str:
    """The 32-hex id in a Notion id or URL, dashed; empty when there is none."""
    m = NOTION_ID.search(value.rsplit("/", 1)[-1].split("?")[0]) or NOTION_ID.search(value)
    return "-".join(m.groups()).lower() if m else ""


def _text(prop: dict | None) -> str:
    """Plain text of any simple property type."""
    if not prop:
        return ""
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in value or []).strip()
    if kind in ("select", "status"):
        return (value or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(o.get("name", "") for o in value or [])
    if kind == "people":
        return ", ".join(p.get("name") or "" for p in value or [] if p.get("name"))
    if kind in ("number", "url", "email"):
        return "" if value is None else str(value)
    if kind == "formula":
        return str((value or {}).get((value or {}).get("type"), "") or "")
    return ""


def _date(prop: dict | None) -> date | None:
    start = ((prop or {}).get("date") or {}).get("start") if (prop or {}).get("type") == "date" else None
    try:
        return date.fromisoformat(start[:10]) if start else None
    except ValueError:
        return None


def page_to_document(page: dict, known_ids: set[str]) -> dict:
    """Register fields from one Notion page. Requirement ids are taken from the Requirements property, if valid."""
    props = {k.strip().lower(): v for k, v in (page.get("properties") or {}).items()}
    title = next((_text(v) for v in props.values() if v.get("type") == "title"), "")
    doc_type = _text(props.get("type")).lower()
    status = _text(props.get("status")).lower()
    return {
        "external_id": page["id"],
        "title": title or "Untitled",
        "doc_type": doc_type if doc_type in TYPES else "other",
        "status": "approved" if status == "approved" else "retired" if status in ("retired", "archived") else "draft",
        "owner": _text(props.get("owner")),
        "version": _text(props.get("version")),
        "approved_by": _text(props.get("approved by")),
        "approved_on": _date(props.get("approved on")),
        "last_review": _date(props.get("last review")),
        "next_review": _date(props.get("next review")),
        "link": page.get("url", ""),
        "requirement_ids": sorted({r for r in REQ_ID.findall(_text(props.get("requirements"))) if r in known_ids}),
    }


def evaluate(docs: list[dict], today: date) -> list:
    approved = [d for d in docs if d["status"] == "approved"]
    register = REGISTER.result(
        PASS if approved else WARN,
        f"{len(docs)} documents in Notion, {len(approved)} approved." if docs else "The Notion database has no documents yet.",
        {"documents": len(docs), "approved": len(approved), "by_type": {t: sum(d["doc_type"] == t for d in docs) for t in TYPES}},
    )
    limit = today - timedelta(days=365 * REVIEW_YEARS)

    def overdue_doc(d: dict) -> bool:
        reviewed = d["last_review"] or d["approved_on"]
        return not reviewed or reviewed < limit or bool(d["next_review"] and d["next_review"] < today)

    overdue = [d["title"] for d in approved if overdue_doc(d)]
    soon = [d["title"] for d in approved if d["next_review"] and today <= d["next_review"] <= today + timedelta(days=30)]
    details = {"overdue": overdue[:50], "due_within_30_days": soon[:50]}
    if not approved:
        reviews = REVIEWS.result(INFO, "No approved documents to review.", details)
    elif overdue:
        reviews = REVIEWS.result(
            FAIL, f"{len(overdue)} of {len(approved)} approved documents are past their review date or not reviewed for {REVIEW_YEARS} years.", details
        )
    elif soon:
        reviews = REVIEWS.result(WARN, f"All approved documents are reviewed; {len(soon)} are due within 30 days.", details)
    else:
        reviews = REVIEWS.result(PASS, f"All {len(approved)} approved documents are within their review period.", details)
    unmapped = [d["title"] for d in docs if d["status"] != "retired" and not d["requirement_ids"]]
    mapping = MAPPING.result(
        WARN if unmapped else PASS,
        f"{len(unmapped)} documents are not linked to a requirement." if unmapped else "Every current document is linked to at least one requirement.",
        {"unmapped": unmapped[:50]},
    )
    return [register, reviews, mapping]


class NotionConnector(Connector):
    key = "notion"
    name = "Notion"
    description = "Document register from a Notion database: policies, procedures, plans, registers and records with approval and review dates."
    env_vars = ("NOTION_TOKEN", "NOTION_DATABASE")
    docs = "docs/connectors.md#notion"

    def configured(self) -> bool:
        return bool(self.config.notion_token and notion_id(self.config.notion_database))

    def _client(self) -> httpx.Client:
        return self.client({"Authorization": f"Bearer {self.config.notion_token}", "Notion-Version": VERSION}, API)

    def _data_source(self, c: httpx.Client) -> tuple[str, str]:
        """(database title, id of its first data source)."""
        db = self.get_json(c, f"/databases/{notion_id(self.config.notion_database)}")
        sources = db.get("data_sources") or []
        if not sources:
            raise RuntimeError("the Notion database has no data source")
        return _text({"type": "title", "title": db.get("title")}) or "Untitled", sources[0]["id"]

    def test(self) -> str:
        with self._client() as c:
            title, source = self._data_source(c)
            r = c.post(f"/data_sources/{source}/query", json={"page_size": 1})
            r.raise_for_status()
        return f"Connected to the Notion database {title}."

    def pages(self, c: httpx.Client, source: str) -> list[dict]:
        pages, cursor = [], None
        while True:
            body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            r = c.post(f"/data_sources/{source}/query", json=body)
            r.raise_for_status()
            data = r.json()
            pages += [p for p in data.get("results", []) if p.get("object") == "page" and not p.get("in_trash") and not p.get("is_archived")]
            if not data.get("has_more") or len(pages) >= 2000:
                return pages
            cursor = data.get("next_cursor")

    def sync(self) -> SyncResult:
        from ..framework import all_requirement_ids

        with self._client() as c:
            title, source = self._data_source(c)
            pages = self.pages(c, source)
        known = set(all_requirement_ids())
        docs = [page_to_document(p, known) for p in pages]
        raw = {"database": title, "documents": [{**d, **{k: d[k] and d[k].isoformat() for k in ("approved_on", "last_review", "next_review")}} for d in docs]}
        return SyncResult(checks=evaluate(docs, date.today()), raw=raw, documents=docs)

    def create_database(self, parent: str, requirement_ids: list[str]) -> str:
        """Create the document database under a page shared with the integration. Returns its id."""
        schema = {**SCHEMA, "Requirements": {"multi_select": {"options": [{"name": r} for r in requirement_ids[:100]]}}}
        body = {
            "parent": {"type": "page_id", "page_id": notion_id(parent)},
            "title": [{"type": "text", "text": {"content": "CyFun documented information"}}],
            "initial_data_source": {"properties": schema},
        }
        with self._client() as c:
            r = c.post("/databases", json=body)
            r.raise_for_status()
            return r.json()["id"]
