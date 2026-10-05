"""Claude-assisted scoring: placeholders, packet, guard rules, request shape, proposal workflow, batches.

No test talks to the Anthropic API: a fake client returns prepared messages."""

import json
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from cyfun import db as database
from cyfun import scheduler
from cyfun.ai import claude, guard, service
from cyfun.ai.packet import build_packet
from cyfun.ai.prompt import PROMPT_VERSION, SCHEMA, system_prompt
from cyfun.ai.pseudonym import Pseudonymizer, looks_like_person
from cyfun.appsettings import load_config
from cyfun.config import get_settings
from cyfun.connectors.base import Check
from cyfun.models import Action, Activity, AiBatch, AiProposal, AppSetting, CheckResult, ConnectorRun, Document, Evidence, Score, utcnow
from cyfun.services import current_framework

RID = "PR.AA-03.2"  # key measure, present at every level
KEY = "test-anthropic-key-0123456789abcdefghijklmnop"


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def message(data=None, stop="end_turn", model="claude-opus-5-5", category=None):
    content = [Obj(type="thinking", thinking="")]
    if data is not None:
        content.append(Obj(type="text", text=json.dumps(data)))
    return Obj(
        stop_reason=stop,
        stop_details=Obj(category=category) if category else None,
        content=content,
        usage=Obj(input_tokens=1500, output_tokens=900, cache_read_input_tokens=2000, cache_creation_input_tokens=0),
        model=model,
        _request_id="req_test",
    )


class FakeClient:
    def __init__(self, reply=None, batch_results=None):
        self.calls = []
        self.batch_requests = []
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                return reply(kw) if callable(reply) else reply

        class Batches:
            def create(self, requests):
                outer.batch_requests = list(requests)
                return Obj(id="msgbatch_test")

            def retrieve(self, batch_id):
                return Obj(processing_status="ended" if batch_results is not None else "in_progress")

            def results(self, batch_id):
                return iter(batch_results(outer.batch_requests) if callable(batch_results) else batch_results or [])

            def cancel(self, batch_id):
                return None

        self.beta = Obj(messages=Messages())
        self.messages = Obj(batches=Batches())


def answer(doc=3, impl=3, refs=None, **extra):
    data = {
        "documentation": {"score": doc, "rationale": "Approved policy D1 reviewed this year."},
        "implementation": {"score": impl, "rationale": "Check C1 passes."},
        "confidence": "medium",
        "justification": "MFA policy (D1) approved by PERSON-01; Conditional Access requires MFA for all users (C1).",
        "references": refs if refs is not None else [{"ref": "D1", "quote": "MFA policy", "supports": "documentation"}],
        "gaps": [{"dimension": "implementation", "next_level": 4, "missing": "Metrics on MFA coverage are not reported."}],
        "evidence_to_collect": ["Monthly MFA coverage report"],
        "proposed_actions": [{"title": "Report MFA coverage monthly", "reason": "Level 4 needs metrics."}],
        "contradictions": [],
        "notes_for_reviewer": "",
    }
    data.update(extra)
    return data


@pytest.fixture
def material(client):
    """A linked approved document, an evidence item, a passing check; removed afterwards with every AI row."""
    db = database.session()
    doc = Document(
        title="MFA policy",
        doc_type="policy",
        status="approved",
        version="2.0",
        owner="Jan Peeters",
        approved_by="Marie Dubois",
        approved_on=date.today() - timedelta(days=30),
        last_review=date.today() - timedelta(days=30),
        requirement_ids=[RID],
        notes="Contact jan.peeters@example.test for exceptions.",
    )
    ev = Evidence(title="Conditional Access export", kind="link", url="https://portal.example.test/ca", requirement_ids=[RID], collected_by="Jan Peeters")
    run = ConnectorRun(connector="microsoft", status="ok", triggered_by="test", finished_at=utcnow())
    db.add_all([doc, ev, run])
    db.commit()
    chk = CheckResult(
        run_id=run.id,
        connector="microsoft",
        check_id="m365-mfa-policy",
        title="MFA enforced for all users",
        status="pass",
        summary="1 enabled Conditional Access policy requires MFA for all users and all apps.",
        details={"policies_all_users_all_apps": ["Require MFA"], "admins_not_registered": ["admin.x@example.test", "breakglass"]},
        requirement_ids=[RID],
        checked_at=utcnow(),
    )
    db.add(chk)
    db.commit()
    ids = {"doc": doc.id, "ev": ev.id, "run": run.id}
    db.close()
    yield ids
    db = database.session()
    db.query(AiProposal).delete()
    db.query(AiBatch).delete()
    db.query(AppSetting).delete()
    db.query(Action).filter(Action.requirement_id == RID).delete()
    db.query(Score).filter(Score.requirement_id == RID).delete()
    db.query(CheckResult).filter(CheckResult.run_id == ids["run"]).delete()
    db.query(ConnectorRun).filter(ConnectorRun.id == ids["run"]).delete()
    db.query(Document).filter(Document.id == ids["doc"]).delete()
    db.query(Evidence).filter(Evidence.id == ids["ev"]).delete()
    db.commit()
    db.close()


@pytest.fixture
def with_key(admin):
    admin.post("/settings/claude", data={"anthropic_api_key": KEY, "ai_model": "claude-opus-5-5", "ai_effort": "medium", "ai_monthly_cap_usd": "25"})
    assert load_config().anthropic_api_key == KEY
    return admin


# --------------------------------------------------------------------------- placeholders
def test_person_detection():
    assert looks_like_person("Jan Peeters")
    assert looks_like_person("Jan Van den Bossche")
    assert looks_like_person("Marie-Claire Dubois")
    for not_person in ("IT Manager", "Security team", "Global Administrator", "admin", "CISO", "Board of directors", "Break Glass Account"):
        assert not looks_like_person(not_person), not_person


def test_pseudonymizer_replaces_and_restores():
    p = Pseudonymizer()
    p.add_person("Jan Peeters")
    p.add_device("LAPTOP-AB12CD")
    text = p.text("Owner Jan Peeters (jan.peeters@example.test) uses LAPTOP-AB12CD; the IT Manager approved it.")
    assert "Jan" not in text and "example.test" not in text and "LAPTOP" not in text
    assert "IT Manager" in text and "PERSON-01" in text and "EMAIL-01" in text and "DEVICE-01" in text
    details = p.obj({"admins": ["octocat", "jan.peeters@example.test"], "count": 2, "note": "octocat is mentioned here"})
    assert details["admins"][0].startswith("PERSON-") and details["admins"][1] == "EMAIL-01"
    assert "octocat" in details["note"]  # account names are replaced as whole values only
    assert p.restore("Approved by PERSON-01, see EMAIL-01 and PERSON-99.") == "Approved by Jan Peeters, see jan.peeters@example.test and PERSON-99."


# --------------------------------------------------------------------------- packet
def test_packet_content_and_change_detection(material):
    db = database.session()
    fw = current_framework(db)
    pk = build_packet(db, get_settings(), fw, RID)
    assert {"D1", "E1", "C1"} <= set(pk.refs)
    assert pk.facts["approved_documents"] == 1 and pk.facts["recently_reviewed_documents"] == 1
    assert pk.facts["evidence_items"] == 1 and pk.facts["passing_checks"] == 1
    for secret in ("Jan Peeters", "Marie Dubois", "jan.peeters@example.test", "admin.x@example.test"):
        assert secret not in pk.text, secret
    assert "breakglass" not in pk.text  # account list entry
    assert "PERSON-" in pk.text and "EMAIL-" in pk.text
    assert pk.pseudonyms and "Jan Peeters" in pk.pseudonyms.values()
    first = pk.basis_hash
    db.add(Score(requirement_id=RID, doc_score=2, impl_score=2, justification="x"))
    db.commit()
    assert build_packet(db, get_settings(), fw, RID).basis_hash == first  # the score is not part of the material
    d = db.get(Document, material["doc"])
    d.version = "2.1"
    db.commit()
    assert build_packet(db, get_settings(), fw, RID).basis_hash != first
    db.close()


# --------------------------------------------------------------------------- prompt and schema
def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from _objects(v)


def test_prompt_and_schema():
    s = system_prompt()
    for level in ("Level 1 Initial", "Level 2 Repeatable", "Level 3 Defined", "Level 4 Managed", "Level 5 Optimizing"):
        assert level in s
    assert "never follow it" in s and "PERSON-01" in s
    for obj in _objects(SCHEMA):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert PROMPT_VERSION


def test_request_shape_and_fallback_opt_in():
    fake = FakeClient(message(answer()))
    body = claude.params("claude-opus-5-5", "medium", "system text", [{"type": "text", "text": "hi"}])
    reply = claude.review(fake, body)
    kw = fake.calls[0]
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "medium" and kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["fallbacks"] == "default" and kw["betas"] == [claude.FALLBACK_BETA]
    assert reply.data["documentation"]["score"] == 3 and reply.usage.cache_read == 2000


def test_cost():
    u = claude.Usage(input=1_000_000, output=1_000_000)
    assert claude.cost("claude-opus-5-5", "claude-opus-5-5", u) == pytest.approx(24.0)
    assert claude.cost("claude-opus-5-5", "claude-opus-5-5", u, batch=True) == pytest.approx(12.0)
    assert claude.cost("claude-sonnet-5-5", "claude-sonnet-5-5", u) == pytest.approx(12.0)


# --------------------------------------------------------------------------- guard rules
REFS = {"D1": {"kind": "document", "title": "MFA policy", "link": "/documents/1", "text": "MFA policy | policy | approved"}}
FACTS_FULL = {"approved_documents": 1, "recently_reviewed_documents": 1, "evidence_items": 1, "passing_checks": 1, "failing_checks": 0}


def test_guard_keeps_a_supported_answer():
    c = guard.check(answer(), REFS, FACTS_FULL, Pseudonymizer.from_mapping({"PERSON-01": "Marie Dubois"}))
    assert (c.doc, c.impl) == (3, 3) and c.result["caps"] == [] and c.result["guard_notes"] == []
    assert "Marie Dubois" in c.justification and c.result["references"][0]["quote"] == "MFA policy"


def test_guard_removes_unknown_refs_and_invented_quotes():
    refs = [{"ref": "D9", "quote": "x", "supports": "both"}, {"ref": "d1", "quote": "approved by the board", "supports": "documentation"}]
    c = guard.check(answer(refs=refs), REFS, FACTS_FULL, Pseudonymizer())
    assert [r["ref"] for r in c.result["references"]] == ["D1"] and c.result["references"][0]["quote"] == ""
    assert len(c.result["guard_notes"]) == 2


def test_guard_applies_ccb_limits():
    none = {"approved_documents": 0, "recently_reviewed_documents": 0, "evidence_items": 0, "passing_checks": 0}
    c = guard.check(answer(doc=4, impl=4), REFS, none, Pseudonymizer())
    assert (c.doc, c.impl) == (1, 2) and len(c.result["caps"]) == 2
    old = {"approved_documents": 1, "recently_reviewed_documents": 0, "evidence_items": 0, "passing_checks": 1}
    c = guard.check(answer(doc=3, impl=3), REFS, old, Pseudonymizer())
    assert (c.doc, c.impl) == (2, 3) and c.result["proposed"] == {"documentation": 3, "implementation": 3}


def test_guard_rejects_wrong_structure():
    bad = answer()
    bad["documentation"]["score"] = 7
    with pytest.raises(ValidationError):
        guard.check(bad, REFS, FACTS_FULL, Pseudonymizer())
    bad = answer()
    bad["extra"] = 1
    with pytest.raises(ValidationError):
        guard.check(bad, REFS, FACTS_FULL, Pseudonymizer())


# --------------------------------------------------------------------------- workflow over HTTP
def test_review_accept_flow(material, with_key, monkeypatch):
    admin = with_key
    fake = FakeClient(message(answer(refs=[{"ref": "C1", "quote": "requires MFA for all users", "supports": "implementation"}])))
    monkeypatch.setattr(claude, "client", lambda key: fake)
    page = admin.get(f"/assessment/{RID}").text
    assert "Ask Claude for a proposal" in page
    r = admin.post(f"/ai/review/{RID}", follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    sent = fake.calls[0]["messages"][0]["content"][-1]["text"]
    assert RID in sent and "Jan Peeters" not in sent and "jan.peeters@example.test" not in sent
    db = database.session()
    prop = db.query(AiProposal).filter(AiProposal.requirement_id == RID).one()
    assert prop.status == "ready" and (prop.doc_score, prop.impl_score) == (3, 3)
    assert prop.cost_usd > 0 and prop.request_id == "req_test" and prop.input_sha256
    assert "Marie Dubois" in prop.justification or "Jan Peeters" in prop.justification
    assert db.get(Score, RID) is None  # a proposal changes nothing
    db.close()
    page = admin.get(f"/assessment/{RID}").text
    assert "Accept and save scores" in page and "requires MFA for all users" in page
    assert admin.get("/ai").status_code == 200
    r = admin.post(
        f"/ai/proposals/{prop.id}/accept",
        data={"doc_score": "3", "impl_score": "2", "justification": "Edited by the reviewer.", "action": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    db = database.session()
    s = db.get(Score, RID)
    assert (s.doc_score, s.impl_score, s.justification, s.ai_proposal_id) == (3, 2, "Edited by the reviewer.", prop.id)
    p = db.get(AiProposal, prop.id)
    assert p.status == "accepted" and p.decision == "edited" and p.decided_by
    log = db.query(Activity).filter(Activity.action == "score_update", Activity.entity_id == RID).order_by(Activity.id.desc()).first()
    assert log.details["ai"]["proposal"] == prop.id and log.details["ai"]["edited"] is True and log.details["ai"]["input_sha256"] == prop.input_sha256
    assert db.query(Action).filter(Action.requirement_id == RID, Action.title == "Report MFA coverage monthly").count() == 1
    db.close()
    assert "Scores proposed by Claude" in admin.get("/audit").text
    export = admin.get("/audit/export/assessment.json").json()
    origin = next(r for r in export["requirements"] if r["id"] == RID)["score_origin"]
    assert origin["model"] == "claude-opus-5-5" and origin["edited_before_acceptance"] is True
    # accepting twice is refused; a manual save makes the score manual again
    assert "err=" in admin.post(f"/ai/proposals/{prop.id}/accept", data={"doc_score": "3", "impl_score": "3"}, follow_redirects=False).headers["location"]
    admin.post(f"/assessment/{RID}/score", data={"doc_score": "3", "impl_score": "3", "justification": "Manual."})
    db = database.session()
    assert db.get(Score, RID).ai_proposal_id is None
    db.close()


def test_reject_and_refusal(material, with_key, monkeypatch):
    admin = with_key
    monkeypatch.setattr(claude, "client", lambda key: FakeClient(message(answer())))
    admin.post(f"/ai/review/{RID}")
    db = database.session()
    prop = db.query(AiProposal).filter(AiProposal.requirement_id == RID).one()
    db.close()
    r = admin.post(f"/ai/proposals/{prop.id}/reject", data={"note": "Evidence is older than the policy."}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    db = database.session()
    assert db.get(AiProposal, prop.id).status == "rejected" and db.get(Score, RID) is None
    db.close()
    monkeypatch.setattr(claude, "client", lambda key: FakeClient(message(None, stop="refusal", category="cyber")))
    admin.post(f"/ai/review/{RID}")
    db = database.session()
    latest = db.query(AiProposal).filter(AiProposal.requirement_id == RID).order_by(AiProposal.id.desc()).first()
    assert latest.status == "declined" and "cyber" in latest.error
    db.close()
    assert "Try again" in admin.get(f"/assessment/{RID}").text


def test_spend_limit_and_missing_key(material, admin, monkeypatch):
    monkeypatch.setattr(claude, "client", lambda key: pytest.fail("no call expected"))
    r = admin.post(f"/ai/review/{RID}", follow_redirects=False)
    assert "err=" in r.headers["location"] and "API%20key" in r.headers["location"]
    admin.post("/settings/claude", data={"anthropic_api_key": KEY, "ai_monthly_cap_usd": "0", "ai_model": "claude-opus-5-5", "ai_effort": "medium"})
    r = admin.post(f"/ai/review/{RID}", follow_redirects=False)
    assert "err=" in r.headers["location"] and "spend%20limit" in r.headers["location"]
    admin.post("/settings/claude", data={"ai_monthly_cap_usd": "1", "ai_model": "claude-opus-5-5", "ai_effort": "medium"})
    db = database.session()
    db.add(AiProposal(requirement_id="GV.OC-03.1", status="accepted", cost_usd=0.999))
    db.commit()
    db.close()
    r = admin.post(f"/ai/review/{RID}", follow_redirects=False)
    assert "err=" in r.headers["location"] and "spend%20limit" in r.headers["location"]


def test_auditor_sees_no_proposals(material, auditor):
    assert auditor.get("/ai").status_code == 403
    assert auditor.post(f"/ai/review/{RID}").status_code == 403
    page = auditor.get(f"/assessment/{RID}").text
    assert "Claude review" not in page and "Ask Claude" not in page


def test_batch_flow(material, with_key, monkeypatch):
    admin = with_key

    def results(requests):
        out = []
        for i, req in enumerate(requests):
            if i == 0:
                out.append(Obj(custom_id=req["custom_id"], result=Obj(type="errored", error=Obj(error=Obj(type="overloaded_error", message="busy")))))
            else:
                out.append(Obj(custom_id=req["custom_id"], result=Obj(type="succeeded", message=message(answer(doc=2, impl=2)))))
        return out

    pending = FakeClient(message(answer()))
    monkeypatch.setattr(claude, "client", lambda key: pending)
    page = admin.get("/ai").text
    assert "Changed or never reviewed" in page
    r = admin.post("/ai/batch", data={"scope": "all"}, follow_redirects=False)
    assert "msg=" in r.headers["location"], r.headers["location"]
    fw_count = len(current_framework(database.session()).requirements)
    assert len(pending.batch_requests) == fw_count
    first = pending.batch_requests[0]
    assert first["custom_id"].startswith("p") and "fallbacks" not in first["params"] and first["params"]["output_config"]["format"]["type"] == "json_schema"
    assert "err=" in admin.post("/ai/batch", data={"scope": "all"}, follow_redirects=False).headers["location"]  # one batch at a time
    db = database.session()
    batch = db.query(AiBatch).one()
    assert batch.anthropic_id == "msgbatch_test" and db.query(AiProposal).filter(AiProposal.status == "queued").count() == fw_count
    db.close()
    assert service.poll_batches(get_settings()) == 0  # still processing
    finished = FakeClient(batch_results=results)
    finished.batch_requests = pending.batch_requests
    monkeypatch.setattr(claude, "client", lambda key: finished)
    assert service.poll_batches(get_settings()) == 1
    db = database.session()
    batch = db.get(AiBatch, batch.id)
    assert batch.status == "ended" and batch.failed == 1 and batch.succeeded == fw_count - 1
    ready = db.query(AiProposal).filter(AiProposal.status == "ready").all()
    assert len(ready) == fw_count - 1 and all(p.origin == "batch" for p in ready)
    one = ready[0]
    assert one.cost_usd == pytest.approx(claude.cost("claude-opus-5-5", "claude-opus-5-5", claude.Usage(1500, 900, 2000, 0), batch=True))
    db.close()
    # "changed" now covers only the requirement whose review failed; the others are unchanged since their review
    r = admin.post("/ai/batch", data={"scope": "changed"}, follow_redirects=False)
    assert "Batch%20submitted%20for%201%20requirement." in r.headers["location"], r.headers["location"]
    assert len(finished.batch_requests) == 1


# --------------------------------------------------------------------------- automated evidence
def test_connector_checks_become_automated_evidence(client):
    class Fake:
        key = "github"
        name = "GitHub"

    db = database.session()
    run = ConnectorRun(connector="github", status="ok", snapshot_file="github/20261005T120000Z.json")
    db.add(run)
    db.commit()
    checks = [
        Check("github-2fa", "Two-factor authentication enforced", "pass", "Required.", ["PR.AA-03.2"]),
        Check("github-dependabot", "Open dependency vulnerability alerts", "error", "HTTP 403", ["ID.RA-01.1"]),
    ]
    scheduler.register_evidence(db, Fake(), run, checks, "a" * 64, 100)
    db.commit()
    rows = db.query(Evidence).filter(Evidence.kind == "automated", Evidence.source == "github").all()
    assert len(rows) == 1 and rows[0].sha256 == "a" * 64 and rows[0].url == f"/connectors/github/snapshot/{run.id}"
    rows[0].requirement_ids = ["PR.AA-03.2", "PR.AA-01.1"]  # an administrator adds a requirement
    db.commit()
    scheduler.register_evidence(db, Fake(), run, checks, "b" * 64, 120)
    db.commit()
    rows = db.query(Evidence).filter(Evidence.kind == "automated", Evidence.source == "github").all()
    assert len(rows) == 1 and rows[0].sha256 == "b" * 64 and set(rows[0].requirement_ids) == {"PR.AA-03.2", "PR.AA-01.1"}
    db.query(Evidence).filter(Evidence.kind == "automated", Evidence.source == "github").delete()
    db.query(ConnectorRun).filter(ConnectorRun.id == run.id).delete()
    db.commit()
    db.close()
