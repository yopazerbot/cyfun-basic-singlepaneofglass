"""Build the audit pack: a ZIP with the assessment state, evidence files and automated check results.

Contents
    README.txt                 what is in the pack and how it was produced
    summary.html               human-readable verification view (self-contained)
    assessment.json            organisation, scope, scores, justifications, mappings
    risk_assessment.json       assurance level risk assessment and risk register
    documents.csv              document register
    assets.csv                 asset inventory (active assets)
    evidence/index.csv         evidence register with SHA-256 hashes
    evidence/<id>_<file>       uploaded evidence files
    checks/latest_checks.json  latest automated check results per connector
    checks/<connector>.json    raw connector snapshot behind those results
    checks/snapshots/<connector>-<file>  snapshot behind each automated evidence item

Scores that came from a proposal by Claude (Anthropic) carry "score_origin" in
assessment.json and a line in summary.html: the model, who accepted it and when.

Not included on purpose: remediation actions, journey notes and the activity log.
Those are internal working data; the auditor verifies the state, not the planning.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .framework import Framework
from .models import AiProposal, Asset, Document, Evidence, RiskItem, Score
from .scoring import summary_to_dict
from .services import current_summary, evidence_path, get_org, get_risk, latest_checks, latest_runs, scores_by_id


def ai_origins(db: Session) -> dict[str, dict]:
    """Requirement id -> origin of scores accepted from a Claude proposal (model, who accepted, when, edited)."""
    scores = db.execute(select(Score).where(Score.ai_proposal_id.is_not(None))).scalars().all()
    if not scores:
        return {}
    ids = [s.ai_proposal_id for s in scores]
    props = {p.id: p for p in db.execute(select(AiProposal).where(AiProposal.id.in_(ids))).scalars().all()}
    out = {}
    for s in scores:
        p = props.get(s.ai_proposal_id)
        if p is None:
            continue
        out[s.requirement_id] = {
            "proposed_by": "Claude (Anthropic)",
            "model": p.served_model or p.model,
            "accepted_by": p.decided_by,
            "accepted_at": p.decided_at.isoformat(timespec="minutes") if p.decided_at else None,
            "edited_before_acceptance": p.decision == "edited",
        }
    return out


def assessment_payload(db: Session, fw: Framework) -> dict:
    org = get_org(db)
    origins = ai_origins(db)
    summary = current_summary(db, fw)
    scores = scores_by_id(db)
    evidence = db.execute(select(Evidence).order_by(Evidence.id)).scalars().all()
    documents = db.execute(select(Document).where(Document.status != "retired").order_by(Document.title)).scalars().all()
    checks = latest_checks(db)
    reqs = []
    for r in fw.requirements:
        s = scores.get(r.id)
        res = summary.requirements[r.id]
        reqs.append(
            {
                "id": r.id,
                "function": r.function_name,
                "category": f"{r.category_name} ({r.category_id})",
                "subcategory": f"{r.subcategory_id}: {r.subcategory_title}",
                "requirement": r.text,
                "key_measure": r.key_measure,
                "level": r.level,
                "management_aspect": r.management_aspect,
                "documentation_score": None if (s is None or s.not_applicable) else s.doc_score,
                "implementation_score": None if (s is None or s.not_applicable) else s.impl_score,
                "not_applicable": bool(s and s.not_applicable),
                "maturity": res.maturity,
                "justification": s.justification if s else "",
                "score_origin": origins.get(r.id),
                "evidence_ids": [e.id for e in evidence if r.id in (e.requirement_ids or [])],
                "document_ids": [d.id for d in documents if r.id in (d.requirement_ids or [])],
                "checks": [
                    {"connector": c.connector, "check": c.check_id, "status": c.status, "summary": c.summary, "checked_at": c.checked_at.isoformat()}
                    for c in checks
                    if r.id in (c.requirement_ids or [])
                ],
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "assurance_level": fw.level,
        "framework": {k: v for k, v in fw.meta.items() if k in ("framework", "level", "owner", "source", "thresholds", "counts")},
        "organisation": {
            "name": org.name,
            "legal_entity": org.legal_entity,
            "enterprise_number": org.enterprise_number,
            "contact": org.contact,
            "scope": org.scope_description,
            "exclusions": org.scope_exclusions,
            "conformity_assessment_body": org.cab_name,
            "self_assessment_date": org.self_assessment_date.isoformat() if org.self_assessment_date else None,
        },
        "summary": summary_to_dict(summary),
        "requirements": reqs,
        "evidence": [
            {
                "id": e.id,
                "title": e.title,
                "kind": e.kind,
                "file_name": e.file_name,
                "url": e.url,
                "sha256": e.sha256,
                "size": e.size,
                "collected_on": e.collected_on.isoformat() if e.collected_on else None,
                "requirements": e.requirement_ids,
            }
            for e in evidence
        ],
        "documents": [
            {
                "id": d.id,
                "title": d.title,
                "type": d.doc_type,
                "version": d.version,
                "status": d.status,
                "owner": d.owner,
                "approved_by": d.approved_by,
                "approved_on": d.approved_on.isoformat() if d.approved_on else None,
                "last_review": d.last_review.isoformat() if d.last_review else None,
                "next_review": d.next_review.isoformat() if d.next_review else None,
                "link": d.link,
                "requirements": d.requirement_ids,
            }
            for d in documents
        ],
    }


def risk_payload(db: Session) -> dict:
    ra = get_risk(db)
    items = db.execute(select(RiskItem).order_by(RiskItem.id)).scalars().all()
    return {
        "assurance_level_assessment": None
        if ra is None
        else {
            "sector": ra.sector_id,
            "organisation_size": ra.organisation_size,
            "matrix": ra.matrix,
            "total_score": ra.total_score,
            "level": ra.level,
            "rationale": ra.rationale,
            "updated_at": ra.updated_at.isoformat(),
        },
        "risk_register": [
            {
                "id": i.id,
                "title": i.title,
                "threat": i.threat,
                "vulnerability": i.vulnerability,
                "assets": i.assets,
                "likelihood": i.likelihood,
                "impact": i.impact,
                "score": i.score,
                "treatment": i.treatment,
                "measures": i.measures,
                "owner": i.owner,
                "status": i.status,
                "review_date": i.review_date.isoformat() if i.review_date else None,
            }
            for i in items
        ],
    }


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def build_pack(db: Session, fw: Framework, settings: Settings, summary_html: str, out) -> None:
    payload = assessment_payload(db, fw)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", __doc__.strip() + f"\n\nGenerated {payload['generated_at']} by CyFun Basic Single Pane of Glass.\n")
        z.writestr("summary.html", summary_html)
        z.writestr("assessment.json", json.dumps(payload, indent=1, ensure_ascii=False))
        z.writestr("risk_assessment.json", json.dumps(risk_payload(db), indent=1, ensure_ascii=False))
        docs = [["id", "title", "type", "version", "status", "owner", "approved_by", "approved_on", "last_review", "next_review", "link", "requirements"]]
        for d in payload["documents"]:
            docs.append(
                [
                    d["id"],
                    d["title"],
                    d["type"],
                    d["version"],
                    d["status"],
                    d["owner"],
                    d["approved_by"],
                    d["approved_on"],
                    d["last_review"],
                    d["next_review"],
                    d["link"],
                    " ".join(d["requirements"]),
                ]
            )
        z.writestr("documents.csv", _csv(docs))
        assets = [["kind", "name", "description", "owner", "location", "classification", "criticality", "primary", "lifecycle", "source", "last_seen"]]
        for a in db.execute(select(Asset).where(Asset.lifecycle != "retired").order_by(Asset.kind, Asset.name)).scalars().all():
            assets.append(
                [
                    a.kind,
                    a.name,
                    a.description,
                    a.owner,
                    a.location,
                    a.classification,
                    a.criticality,
                    "yes" if a.primary_asset else "no",
                    a.lifecycle,
                    a.source,
                    a.last_seen_at.isoformat() if a.last_seen_at else "",
                ]
            )
        z.writestr("assets.csv", _csv(assets))
        index = [["id", "title", "kind", "file", "url", "sha256", "collected_on", "requirements"]]
        added: set[str] = set()
        for e in db.execute(select(Evidence).order_by(Evidence.id)).scalars().all():
            fname = ""
            if e.kind == "automated" and e.source and e.file_name:
                p = (settings.snapshots_dir / e.source / e.file_name).resolve()
                if settings.snapshots_dir.resolve() in p.parents and p.exists():
                    fname = f"checks/snapshots/{e.source}-{e.file_name}"
                    if fname not in added:
                        z.write(p, fname)
                        added.add(fname)
            if e.kind == "file" and e.stored_name:
                try:
                    p = evidence_path(settings, e)
                except ValueError:
                    p = None
                if p and p.exists():
                    fname = f"{e.id}_{e.file_name or e.stored_name}"
                    z.write(p, f"evidence/{fname}")
            index.append(
                [e.id, e.title, e.kind, fname, e.url, e.sha256, e.collected_on.isoformat() if e.collected_on else "", " ".join(e.requirement_ids or [])]
            )
        z.writestr("evidence/index.csv", _csv(index))
        checks = latest_checks(db)
        z.writestr(
            "checks/latest_checks.json",
            json.dumps(
                [
                    {
                        "connector": c.connector,
                        "check": c.check_id,
                        "title": c.title,
                        "status": c.status,
                        "summary": c.summary,
                        "requirements": c.requirement_ids,
                        "checked_at": c.checked_at.isoformat(),
                        "details": c.details,
                    }
                    for c in checks
                ],
                indent=1,
                ensure_ascii=False,
            ),
        )
        for key, run in latest_runs(db).items():
            if run.status == "ok" and run.snapshot_file:
                p = (settings.snapshots_dir / run.snapshot_file).resolve()
                if settings.snapshots_dir.resolve() in p.parents and p.exists():
                    z.write(p, f"checks/{key}.json")
