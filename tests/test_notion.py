"""Notion connector: property mapping, checks, register sync with retirement, read-only entries, database creation."""

import json
from datetime import date, timedelta

import httpx
import pytest

from cyfun import db as database
from cyfun import scheduler
from cyfun.appsettings import load_config, save_group
from cyfun.config import get_settings
from cyfun.connectors import notion
from cyfun.connectors.notion import NotionConnector, evaluate, notion_id, page_to_document
from cyfun.models import AppSetting, Document

DB_ID = "1a2b3c4d5e6f40718293a4b5c6d7e8f9"
SOURCE_ID = "99999999-8888-7777-6666-555555555555"
TODAY = date.today()


def _page(pid: str, title: str, status="Approved", reqs="GV.PO-01.1, PR.AA-01.1, XX.YY-99.9", last_review=None, next_review=None, **extra) -> dict:
    props = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
        "type": {"type": "select", "select": {"name": "Policy"}},
        "Status": {"type": "status", "status": {"name": status}},
        "Owner": {"type": "people", "people": [{"name": "Yoshi"}, {"id": "no-name"}]},
        "Version": {"type": "rich_text", "rich_text": [{"plain_text": "1.2"}]},
        "Last review": {"type": "date", "date": {"start": (last_review or TODAY - timedelta(days=30)).isoformat()}},
        "Next review": {"type": "date", "date": {"start": next_review.isoformat()} if next_review else None},
        "Requirements": {"type": "multi_select", "multi_select": [{"name": r.strip()} for r in reqs.split(",") if r.strip()]},
    }
    return {"object": "page", "id": pid, "url": f"https://www.notion.so/{pid.replace('-', '')}", "properties": props, **extra}


def test_notion_id_from_links_and_ids():
    assert notion_id(DB_ID) == "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"
    assert notion_id(f"https://www.notion.so/olive/CyFun-docs-{DB_ID}?v=0123456789abcdef0123456789abcdef") == "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"
    assert notion_id("no id here") == ""


def test_page_mapping_is_tolerant():
    d = page_to_document(_page("p1", "Information security policy"), {"GV.PO-01.1", "PR.AA-01.1"})
    assert (d["title"], d["doc_type"], d["status"], d["owner"], d["version"]) == ("Information security policy", "policy", "approved", "Yoshi", "1.2")
    assert d["requirement_ids"] == ["GV.PO-01.1", "PR.AA-01.1"]  # unknown ids dropped
    assert d["next_review"] is None and d["link"].startswith("https://www.notion.so/")
    bare = page_to_document({"id": "p2", "properties": {"Titel": {"type": "title", "title": []}}}, set())
    assert (bare["title"], bare["status"], bare["doc_type"]) == ("Untitled", "draft", "other")


def test_review_and_mapping_checks():
    known = {"GV.PO-01.1"}
    ok = page_to_document(_page("a", "A", next_review=TODAY + timedelta(days=200)), known)
    soon = page_to_document(_page("b", "B", next_review=TODAY + timedelta(days=10)), known)
    late = page_to_document(_page("c", "C", next_review=TODAY - timedelta(days=1)), known)
    old = page_to_document(_page("d", "D", last_review=TODAY - timedelta(days=800)), known)
    draft = page_to_document(_page("e", "E", status="Draft", reqs=""), known)
    status = lambda docs: [c.status for c in evaluate(docs, TODAY)]  # noqa: E731
    assert status([ok]) == ["pass", "pass", "pass"]
    assert status([ok, soon]) == ["pass", "warn", "pass"]
    assert status([ok, late])[1] == "fail" and status([old])[1] == "fail"
    assert status([draft]) == ["warn", "info", "warn"]
    assert status([]) == ["warn", "info", "pass"]


@pytest.fixture
def notion_configured(client):
    with database.session() as db:
        changes, errors = save_group(db, get_settings(), "notion", {"notion_token": "ntn_test_secret_0123456789abcdef", "notion_database": DB_ID}, "test")
        assert not errors
    yield
    with database.session() as db:
        db.query(AppSetting).filter(AppSetting.key.like("notion_%")).delete(synchronize_session=False)
        db.query(Document).filter(Document.source == "notion").delete(synchronize_session=False)
        db.commit()


def _mock(monkeypatch, pages: list[dict], seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Notion-Version"] == notion.VERSION
        assert request.headers["Authorization"] == "Bearer ntn_test_secret_0123456789abcdef"
        if seen is not None:
            seen.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.method == "GET" and request.url.path.startswith("/v1/databases/"):
            return httpx.Response(200, json={"title": [{"plain_text": "CyFun docs"}], "data_sources": [{"id": SOURCE_ID, "name": "Docs"}]})
        if request.url.path == f"/v1/data_sources/{SOURCE_ID}/query":
            body = json.loads(request.content)
            start = int(body.get("start_cursor") or 0)
            chunk = pages[start : start + 2]  # two per page to exercise the cursor
            more = start + 2 < len(pages)
            return httpx.Response(200, json={"results": chunk, "has_more": more, "next_cursor": str(start + 2) if more else None})
        if request.method == "POST" and request.url.path == "/v1/databases":
            return httpx.Response(200, json={"id": "abcdefab-cdef-abcd-efab-cdefabcdefab"})
        return httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(
        NotionConnector,
        "client",
        staticmethod(lambda headers=None, base_url="": real(base_url=base_url, headers=headers or {}, transport=httpx.MockTransport(handler))),
    )


def test_sync_fills_register_and_retires_removed_pages(admin, notion_configured, monkeypatch):
    pages = [_page("p1", "Information security policy"), _page("p2", "Backup plan", status="Draft"), _page("p3", "Old page", in_trash=True)]
    _mock(monkeypatch, pages)
    assert NotionConnector(load_config()).configured()
    scheduler.run_connector(get_settings(), "notion", "test")
    with database.session() as db:
        docs = {d.external_id: d for d in db.query(Document).filter(Document.source == "notion")}
    assert set(docs) == {"p1", "p2"}
    assert docs["p1"].status == "approved" and docs["p1"].requirement_ids == ["GV.PO-01.1", "PR.AA-01.1"]

    page = admin.get("/documents").text
    assert "Information security policy" in page and "Notion" in page
    r = admin.post(f"/documents/{docs['p1'].id}", data={"title": "changed in app"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = admin.post(f"/documents/{docs['p1'].id}/delete", follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert "Maintained in Notion" in admin.get(f"/documents/{docs['p1'].id}").text

    _mock(monkeypatch, pages[:1])
    scheduler.run_connector(get_settings(), "notion", "test")
    with database.session() as db:
        p2 = db.query(Document).filter(Document.external_id == "p2").one()
        assert p2.status == "retired" and "Removed from Notion" in p2.notes
        assert db.query(Document).filter(Document.external_id == "p1").one().title == "Information security policy"


def test_create_database_from_settings(admin, client, monkeypatch):
    seen: list = []
    _mock(monkeypatch, [], seen)
    with database.session() as db:
        save_group(db, get_settings(), "notion", {"notion_token": "ntn_test_secret_0123456789abcdef"}, "test")
    try:
        r = admin.post(
            "/settings/notion/create-database",
            data={"notion_parent": "https://www.notion.so/Compliance-0123456789abcdef0123456789abcdef"},
            follow_redirects=False,
        )
        assert "msg=" in r.headers["location"], r.headers["location"]
        _, path, body = next(s for s in seen if s[1] == "/v1/databases")
        assert body["parent"] == {"type": "page_id", "page_id": "01234567-89ab-cdef-0123-456789abcdef"}
        props = body["initial_data_source"]["properties"]
        assert {"Name", "Type", "Status", "Owner", "Next review", "Requirements"} <= set(props)
        assert {"name": "GV.PO-01.1"} in props["Requirements"]["multi_select"]["options"]
        assert notion_id(load_config().notion_database) == "abcdefab-cdef-abcd-efab-cdefabcdefab"
    finally:
        with database.session() as db:
            db.query(AppSetting).filter(AppSetting.key.like("notion_%")).delete(synchronize_session=False)
            db.commit()
