import io
import json
import re
import zipfile


def test_health_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_anonymous_is_redirected_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/auth/login?next=")
    r = client.get("/assessment/PR.AA-01.1", follow_redirects=False)
    assert r.status_code == 302


def test_login_page_offers_local_form_without_sso(client):
    r = client.get("/auth/login")
    assert r.status_code == 200
    assert 'action="/auth/local"' in r.text
    assert "Sign in with Microsoft" not in r.text
    r = client.get("/auth/microsoft", follow_redirects=False)
    assert r.status_code == 503


def test_security_headers(admin):
    r = admin.get("/")
    assert r.status_code == 200
    h = r.headers
    assert h["content-security-policy"].startswith("default-src 'self'")
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["cache-control"] == "no-store"


def test_dashboard_and_pages_render(admin):
    for path in (
        "/",
        "/journey",
        "/risk",
        "/risk/register",
        "/assessment",
        "/assessment?level=km",
        "/assessment?level=below",
        "/assessment/PR.AA-03.2",
        "/assets",
        "/documents",
        "/evidence",
        "/actions",
        "/connectors",
        "/connectors/github",
        "/audit",
        "/audit/export",
        "/activity",
        "/auth/users",
    ):
        r = admin.get(path)
        assert r.status_code == 200, path
    assert "Key measure" in admin.get("/assessment/PR.AA-03.2").text


def test_post_without_origin_is_blocked(admin):
    admin.headers.pop("Origin", None)
    r = admin.post("/assessment/GV.OC-03.1/score", data={"doc_score": "3", "impl_score": "3"}, follow_redirects=False)
    assert r.status_code == 403
    admin.headers["Origin"] = "https://evil.example"
    r = admin.post("/assessment/GV.OC-03.1/score", data={"doc_score": "3", "impl_score": "3"}, follow_redirects=False)
    assert r.status_code == 403


def test_score_save_and_rules(admin):
    r = admin.post(
        "/assessment/GV.OC-03.1/score",
        data={"doc_score": "3", "impl_score": "4", "justification": "Policy v2 approved 2026-01-10."},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    page = admin.get("/assessment/GV.OC-03.1").text
    assert "Policy v2 approved" in page
    assert "3,50" in page

    # key measure cannot be N/A
    r = admin.post("/assessment/PR.DS-11.1/score", data={"not_applicable": "1", "justification": "x"}, follow_redirects=False)
    assert "err=" in r.headers["location"]

    # one N/A allowed at BASIC, second refused
    r = admin.post("/assessment/PR.AA-06.1/score", data={"not_applicable": "1", "justification": "No premises."}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    r = admin.post("/assessment/PR.AA-03.1/score", data={"not_applicable": "1", "justification": "No wifi."}, follow_redirects=False)
    assert "err=" in r.headers["location"]

    # out of range
    r = admin.post("/assessment/GV.RM-03.1/score", data={"doc_score": "9", "impl_score": "1"}, follow_redirects=False)
    assert "err=" in r.headers["location"]

    # activity recorded
    assert "score_update" in admin.get("/activity").text


def test_auditor_is_read_only(auditor):
    assert auditor.get("/assessment").status_code == 200
    r = auditor.post("/assessment/GV.OC-03.1/score", data={"doc_score": "1", "impl_score": "1"}, follow_redirects=False)
    assert r.status_code == 403
    r = auditor.post("/actions", data={"title": "x"}, follow_redirects=False)
    assert r.status_code == 403
    assert auditor.get("/auth/users", follow_redirects=False).status_code == 403
    assert auditor.get("/audit/export/pack.zip").status_code == 200


def test_evidence_upload_download_and_pack(admin):
    files = {"file": ("backup-report.txt", b"restore test passed 2026-09-30", "text/plain")}
    r = admin.post(
        "/assessment/PR.DS-11.1/evidence", data={"title": "Restore test report", "description": "Monthly restore"}, files=files, follow_redirects=False
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    page = admin.get("/evidence").text
    assert "Restore test report" in page
    # bad extension refused
    r = admin.post(
        "/evidence",
        data={"title": "x", "requirement_ids": ["PR.DS-11.1"]},
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        follow_redirects=False,
    )
    assert "err=" in r.headers["location"]
    # download
    m = re.search(r"/evidence/(\d+)/download", page)
    ev_id = int(m.group(1)) if m else None
    assert ev_id is not None
    d = admin.get(f"/evidence/{ev_id}/download")
    assert d.status_code == 200 and d.content == b"restore test passed 2026-09-30"
    assert "attachment" in d.headers["content-disposition"]
    # audit pack contains the file and the registers
    z = zipfile.ZipFile(io.BytesIO(admin.get("/audit/export/pack.zip").content))
    names = z.namelist()
    assert "summary.html" in names and "assessment.json" in names and "evidence/index.csv" in names
    assert any(n.startswith(f"evidence/{ev_id}_") for n in names)
    payload = json.loads(z.read("assessment.json"))
    assert payload["assurance_level"] == "BASIC"
    assert len(payload["requirements"]) == 34
    assert payload["evidence"][0]["sha256"]


def test_risk_assessment_save(admin):
    data = {"sector_id": "II.6_Digital providers", "organisation_size": "1", "rationale": "Small SaaS provider."}
    r = admin.post("/risk", data=data, follow_redirects=False)
    assert r.status_code == 303 and "BASIC" in r.headers["location"]
    page = admin.get("/risk").text
    assert "Small SaaS provider." in page
    r = admin.post("/risk/preview", data=data)
    assert r.status_code == 200 and "BASIC" in r.text


def test_crud_registers(admin):
    r = admin.post(
        "/documents",
        data={
            "title": "Information security policy",
            "doc_type": "policy",
            "status": "approved",
            "version": "1.2",
            "approved_on": "2026-01-10",
            "next_review": "2027-01-10",
            "requirement_ids": ["GV.PO-01.1", "GV.OC-03.1", "GV.SC-05.2"],
        },
        follow_redirects=False,
    )
    assert "msg=" in r.headers["location"]
    assert "Information security policy" in admin.get("/assessment/GV.PO-01.1").text
    r = admin.post(
        "/assets",
        data={"kind": "cloud", "name": "Production tenant", "classification": "Confidential", "criticality": "High", "primary_asset": "1"},
        follow_redirects=False,
    )
    assert "msg=" in r.headers["location"]
    assert "Production tenant" in admin.get("/assets").text
    r = admin.post(
        "/actions",
        data={"title": "Enable MFA for contractors", "requirement_id": "PR.AA-03.2", "priority": "high", "due_date": "2026-11-01"},
        follow_redirects=False,
    )
    assert "msg=" in r.headers["location"]
    assert "Enable MFA for contractors" in admin.get("/").text
    r = admin.post(
        "/risk/register", data={"title": "Ransomware on file server", "likelihood": "2", "impact": "3", "treatment": "mitigate"}, follow_redirects=False
    )
    assert "msg=" in r.headers["location"]
    assert "Ransomware on file server" in admin.get("/risk/register").text


def test_snapshot_and_workbook_export_validation(admin):
    r = admin.post("/audit/snapshot", data={"name": "Before verification"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    page = admin.get("/audit").text
    assert "Before verification" in page
    r = admin.post("/audit/export/workbook", files={"template": ("wrong.xlsx", b"not a workbook", "application/octet-stream")}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_logout_clears_session(admin):
    r = admin.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 303
    admin.cookies.clear()
    assert admin.get("/", follow_redirects=False).status_code == 302


def test_register_pages_offer_add_and_delete_in_the_form(admin):
    admin.post("/documents", data={"title": "UX delete check"})
    page = admin.get("/documents").text
    assert 'href="#document-form"' in page
    from cyfun import db as database
    from cyfun.models import Document

    with database.session() as db:
        did = db.query(Document).filter(Document.title == "UX delete check").one().id
    edit = admin.get(f"/documents/{did}").text
    assert f'formaction="/documents/{did}/delete"' in edit
    r = admin.post(f"/documents/{did}/delete", follow_redirects=False)
    assert "msg=" in r.headers["location"]


def test_activity_labels_and_details_in_words():
    from cyfun.views import action_label, details_text

    assert action_label("score_update") == "Scores changed"
    assert action_label("some_new_event") == "Some new event"
    assert (
        details_text({"before": {"doc": 2, "impl": 1}, "files": 3, "pruned": [], "changes": [{"a": 1}]}) == "before: doc 2, impl 1 · files: 3 · changes: 1 item"
    )
