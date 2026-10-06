"""The requirement packet: everything Claude sees about one requirement, with placeholders for personal data.

Contents: the requirement and its CCB context, the current score, linked documents (D), the titles
of the other register documents (O), linked evidence (E), the latest automated checks (C), open
actions (A), the risk register for ID.RA requirements (R) and an inventory summary for ID.AM
requirements. Every item has a ref so Claude can cite it and the guard rules can check the
citation. Lists inside check details longer than 25 entries are shortened and say so.

`basis_hash` covers the material, not the current score, and drives "review changed requirements".
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..connectors import ALL as CONNECTORS
from ..framework import Framework
from ..models import Action, Asset, Document, Evidence, Organisation, RiskItem, Score, User
from ..services import evidence_path, latest_checks
from ..views import fmt_score
from .pseudonym import Pseudonymizer

CONNECTOR_NAMES = {c.key: c.name for c in CONNECTORS}
REVIEW_WINDOW_DAYS = 730  # CCB documentation level 3: reviewed in the previous 2 years
LIST_LIMIT = 25
STRING_LIMIT = 600
OTHER_DOCS_LIMIT = 60
RISK_LIMIT = 40

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
TEXT_TYPES = {".txt", ".md", ".csv", ".json", ".xml", ".log", ".yaml", ".yml", ".html", ".eml"}
PDF_LIMIT = 10 * 1024 * 1024
IMAGE_LIMIT = 5 * 1024 * 1024
TEXT_LIMIT = 100 * 1024
BINARY_BUDGET = 20 * 1024 * 1024
TEXT_BUDGET = 300 * 1024


@dataclass
class Packet:
    rid: str
    level: str
    text: str  # what is sent: packet JSON plus shared text files, with placeholders
    refs: dict[str, dict]  # ref -> {kind, title, link, text}
    facts: dict  # inputs of the guard rules
    attachments: list[dict] = field(default_factory=list)  # PDF and image evidence sent as content blocks
    basis_hash: str = ""
    pseudonyms: dict[str, str] = field(default_factory=dict)  # placeholder -> original

    @property
    def meta(self) -> dict:
        return {"refs": self.refs, "facts": self.facts, "attachments": self.attachments}


def _iso(d) -> str | None:
    return d.isoformat() if d else None


def _compact(item: dict) -> dict:
    """Leave out empty fields."""
    return {k: v for k, v in item.items() if v not in (None, "")}


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _trim(value, notes: list[str], path: str = ""):
    """Shorten long lists and strings in connector details, and record where."""
    if isinstance(value, list):
        if len(value) > LIST_LIMIT:
            notes.append(f"{path or 'list'}: first {LIST_LIMIT} of {len(value)} entries")
            return [_trim(v, notes, path) for v in value[:LIST_LIMIT]] + [f"... {len(value) - LIST_LIMIT} more"]
        return [_trim(v, notes, path) for v in value]
    if isinstance(value, dict):
        return {k: _trim(v, notes, f"{path}.{k}" if path else k) for k, v in value.items()}
    if isinstance(value, str) and len(value) > STRING_LIMIT:
        notes.append(f"{path or 'text'}: first {STRING_LIMIT} of {len(value)} characters")
        return value[:STRING_LIMIT] + " [shortened]"
    return value


def _flat(value) -> str:
    """All string values of an item, for checking quotes."""
    if isinstance(value, dict):
        return " | ".join(_flat(v) for v in value.values() if v not in (None, "", [], {}))
    if isinstance(value, list):
        return " | ".join(_flat(v) for v in value)
    return str(value)


def normalise(text: str) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def pseudonymizer_for(db: Session) -> Pseudonymizer:
    """Register every person, account and device the application knows, in a stable order."""
    p = Pseudonymizer()
    for u in db.execute(select(User).order_by(User.id)).scalars():
        p.add_person(u.display_name)
        p.add_person(u.email)
    org = db.get(Organisation, 1)
    if org is not None:
        for part in re.split(r"[,;/]| - ", org.contact or ""):
            p.add_person(part)
    for d in db.execute(select(Document).order_by(Document.id)).scalars():
        p.add_person(d.owner)
        p.add_person(d.approved_by)
    for e in db.execute(select(Evidence).order_by(Evidence.id)).scalars():
        p.add_person(e.collected_by)
    for a in db.execute(select(Action).order_by(Action.id)).scalars():
        p.add_person(a.owner)
    for r in db.execute(select(RiskItem).order_by(RiskItem.id)).scalars():
        p.add_person(r.owner)
    for s in db.execute(select(Score).order_by(Score.requirement_id)).scalars():
        p.add_person(s.updated_by)
    for a in db.execute(select(Asset).order_by(Asset.id)).scalars():
        p.add_person(a.owner)
        if a.kind == "hardware":
            p.add_device(a.name)
        elif a.kind == "identity":
            p.add_person(a.name)
            p.add_person(a.description)
    return p


def evidence_file(settings: Settings, e: Evidence | None) -> Path | None:
    """The stored file of an evidence item, or None when it is missing."""
    if e is None:
        return None
    try:
        path = evidence_path(settings, e)
    except ValueError:
        return None
    return path if path.exists() else None


def _attachment(settings: Settings, e: Evidence, used: dict) -> tuple[str, dict | None, str]:
    """Decide how a shared evidence file is sent. Returns (status for the packet, binary attachment, text content)."""
    if e.kind != "file" or not e.stored_name:
        return "", None, ""
    if not e.share_with_ai:
        return "not shared with Claude (metadata only)", None, ""
    ext = Path(e.file_name or e.stored_name).suffix.lower()
    path = evidence_file(settings, e)
    if path is None:
        return "not attached: file missing", None, ""
    size = path.stat().st_size
    if ext == ".pdf" or ext in IMAGE_TYPES:
        limit = PDF_LIMIT if ext == ".pdf" else IMAGE_LIMIT
        if size > limit:
            return f"not attached: larger than {limit // (1024 * 1024)} MB", None, ""
        if used["binary"] + size > BINARY_BUDGET:
            return "not attached: the 20 MB attachment budget of one review is used by other files", None, ""
        used["binary"] += size
        kind = "pdf" if ext == ".pdf" else "image"
        media = "application/pdf" if ext == ".pdf" else IMAGE_TYPES[ext]
        return f"attached as {kind}", {"evidence_id": e.id, "type": kind, "media_type": media, "size": size, "sha256": e.sha256}, ""
    if ext in TEXT_TYPES:
        if size > TEXT_LIMIT:
            return "not attached: text file larger than 100 KB", None, ""
        if used["text"] + size > TEXT_BUDGET:
            return "not attached: the 300 KB text budget of one review is used by other files", None, ""
        used["text"] += size
        return "attached as text below", None, path.read_text(encoding="utf-8", errors="replace")
    return f"not attached: Claude cannot read {ext or 'this file type'} here (PDF, images and text files only)", None, ""


def build_packet(db: Session, settings: Settings, fw: Framework, rid: str, today: date | None = None, pseudo: Pseudonymizer | None = None) -> Packet:
    """Build the packet. A shared `pseudo` keeps placeholders consistent across the packets of one batch."""
    req = fw.get(rid)
    if req is None:
        raise ValueError(f"{rid} is not part of {fw.level}")
    today = today or date.today()
    p = pseudo or pseudonymizer_for(db)
    refs: dict[str, dict] = {}
    basis: dict = {"requirement": rid, "level": fw.level}
    shortened: list[str] = []

    def add_ref(prefix: str, n: int, kind: str, title: str, item: dict, link: str = "") -> str:
        ref = f"{prefix}{n}"
        refs[ref] = {"kind": kind, "title": title, "link": link, "text": _flat(item)}
        return ref

    target = fw.thresholds["key_measure_min"] if req.key_measure else fw.thresholds["total_min"]
    score = db.get(Score, rid)
    packet: dict = {
        "assessment": {
            "assurance_level": fw.level,
            "date": today.isoformat(),
            "minimum_for_this_requirement": f"{fmt_score(target)} ({'key measure minimum' if req.key_measure else 'total maturity minimum of the level'})",
            "category_minimum": fmt_score(fw.thresholds["category_min"]) if fw.thresholds.get("category_min") else "none",
        },
        "requirement": {
            "id": req.id,
            "statement": req.text,
            "introduced_at_level": req.level,
            "key_measure": req.key_measure,
            "management_aspect": req.management_label or None,
            "function": f"{req.function_id} {req.function_name}",
            "category": f"{req.category_id} {req.category_name}",
            "subcategory": f"{req.subcategory_id} {req.subcategory_title}",
            "goal_from_ccb_booklet": req.goal or None,
            "implementation_notes": req.guidance or None,
            "typical_evidence": req.evidence_examples or None,
        },
        "current_score": None
        if score is None
        else {
            "documentation": score.doc_score,
            "implementation": score.impl_score,
            "not_applicable": score.not_applicable,
            "justification": p.text(score.justification) or None,
            "last_changed": _iso(score.updated_at.date() if score.updated_at else None),
        },
    }

    # documents -----------------------------------------------------------------------------
    all_docs = db.execute(select(Document).where(Document.status != "retired").order_by(Document.title)).scalars().all()
    linked = [d for d in all_docs if rid in (d.requirement_ids or [])]
    docs_out = []
    approved = recent = 0
    for i, d in enumerate(linked, 1):
        item = {
            "title": p.text(d.title),
            "type": d.doc_type,
            "status": d.status,
            "version": d.version,
            "owner": p.text(d.owner),
            "approved_by": p.text(d.approved_by),
            "approved_on": _iso(d.approved_on),
            "last_review": _iso(d.last_review),
            "next_review": _iso(d.next_review),
            "stored_at": _host(d.link),
            "notes": p.text(d.notes),
        }
        item = _compact(item)
        docs_out.append({"ref": add_ref("D", i, "document", d.title, item, f"/documents/{d.id}"), **item})
        if d.status == "approved":
            approved += 1
            reviewed = d.last_review or d.approved_on
            if reviewed and reviewed >= today - timedelta(days=REVIEW_WINDOW_DAYS):
                recent += 1
    packet["documents_linked_to_this_requirement"] = docs_out
    others = [d for d in all_docs if rid not in (d.requirement_ids or [])][:OTHER_DOCS_LIMIT]
    other_out = []
    for i, d in enumerate(others, 1):
        item = _compact({"title": p.text(d.title), "type": d.doc_type, "status": d.status, "last_review": _iso(d.last_review or d.approved_on)})
        other_out.append({"ref": add_ref("O", i, "document", d.title, item, f"/documents/{d.id}"), **item})
    if other_out:
        packet["other_documents_in_register"] = other_out
    basis["documents"] = [
        [d.id, d.title, d.doc_type, d.status, d.version, d.owner, d.approved_by, _iso(d.approved_on), _iso(d.last_review), _iso(d.next_review), d.notes]
        for d in linked
    ]
    basis["other_documents"] = [[d.id, d.title, d.status, _iso(d.last_review)] for d in others]

    # evidence ------------------------------------------------------------------------------
    evidence = [e for e in db.execute(select(Evidence).order_by(Evidence.id)).scalars().all() if rid in (e.requirement_ids or []) and e.kind != "automated"]
    used = {"binary": 0, "text": 0}
    ev_out = []
    attachments: list[dict] = []
    texts: list[str] = []
    for i, e in enumerate(evidence, 1):
        status, att, content = _attachment(settings, e, used)
        item = {
            "title": p.text(e.title),
            "kind": e.kind,
            "description": p.text(e.description),
            "collected_on": _iso(e.collected_on),
            "collected_by": p.text(e.collected_by),
            "file": p.text(e.file_name),
            "size_kb": round(e.size / 1024) if e.size else None,
            "link_host": _host(e.url) if e.kind == "link" else None,
            "content": status,
        }
        item = _compact(item)
        ref = add_ref("E", i, "evidence", e.title, item, f"/evidence/{e.id}")
        ev_out.append({"ref": ref, **item})
        if att is not None:
            attachments.append({"ref": ref, **att})
        if content:
            safe = p.text(content)
            refs[ref]["text"] += " | " + safe
            texts.append(f'<evidence_file ref="{ref}" name="{p.text(e.file_name)}">\n{safe}\n</evidence_file>')
    packet["evidence_linked_to_this_requirement"] = ev_out
    basis["evidence"] = [[e.id, e.title, e.kind, e.description, _iso(e.collected_on), e.sha256, e.url, e.share_with_ai] for e in evidence]

    # automated checks ----------------------------------------------------------------------
    checks = [c for c in latest_checks(db) if rid in (c.requirement_ids or [])]
    ch_out = []
    passing = failing = 0
    for i, c in enumerate(checks, 1):
        notes: list[str] = []
        details = p.obj(_trim(c.details or {}, notes))
        item = {
            "system": CONNECTOR_NAMES.get(c.connector, c.connector),
            "check": c.title,
            "status": c.status,
            "summary": p.text(c.summary),
            "checked_on": _iso(c.checked_at.date() if c.checked_at else None),
            "details": details or None,
        }
        if notes:
            item["shortened"] = notes
            shortened.extend(notes)
        item = _compact(item)
        ch_out.append({"ref": add_ref("C", i, "check", f"{item['system']}: {c.title}", item, f"/connectors/{c.connector}"), **item})
        passing += c.status == "pass"
        failing += c.status == "fail"
    packet["automated_checks"] = ch_out
    basis["checks"] = [[c.connector, c.check_id, c.status, c.summary] for c in checks]

    # actions -------------------------------------------------------------------------------
    actions = db.execute(select(Action).where(Action.requirement_id == rid, Action.status.in_(["open", "in_progress"])).order_by(Action.id)).scalars().all()
    act_out = []
    for i, a in enumerate(actions, 1):
        item = _compact({"title": p.text(a.title), "status": a.status, "owner": p.text(a.owner), "due": _iso(a.due_date)})
        act_out.append({"ref": add_ref("A", i, "action", a.title, item, f"/actions/{a.id}"), **item})
    if act_out:
        packet["open_actions"] = act_out
    basis["actions"] = [[a.id, a.title, a.status] for a in actions]

    # risk register for risk-assessment requirements -----------------------------------------
    if rid.startswith(("ID.RA", "GV.RM")):
        risks = db.execute(select(RiskItem).order_by(RiskItem.id)).scalars().all()
        r_out = []
        for i, r in enumerate(risks[:RISK_LIMIT], 1):
            item = {
                "title": p.text(r.title),
                "likelihood_1_3": r.likelihood,
                "impact_1_3": r.impact,
                "treatment": r.treatment,
                "status": r.status,
                "owner": p.text(r.owner),
                "review_date": _iso(r.review_date),
            }
            item = _compact(item)
            r_out.append({"ref": add_ref("R", i, "risk", r.title, item, "/risk/register"), **item})
        packet["risk_register"] = {"items": len(risks), "listed": r_out}
        basis["risks"] = [[r.id, r.title, r.likelihood, r.impact, r.treatment, r.status, _iso(r.review_date)] for r in risks]

    # inventory summary for asset-management requirements -------------------------------------
    if rid.startswith("ID.AM"):
        assets = db.execute(select(Asset).where(Asset.lifecycle != "retired")).scalars().all()
        summary = {
            "active_assets": len(assets),
            "by_kind": dict(Counter(a.kind for a in assets)),
            "by_source": dict(Counter(a.source for a in assets)),
            "with_owner": sum(1 for a in assets if a.owner),
            "marked_primary": sum(1 for a in assets if a.primary_asset),
            "criticality_high": sum(1 for a in assets if a.criticality == "High"),
        }
        packet["asset_inventory_summary"] = summary
        basis["assets"] = summary

    if shortened:
        packet["note"] = "Some lists in check details are shortened; each check names what was shortened."

    body = json.dumps(packet, ensure_ascii=False, indent=1)
    text = f"<requirement_packet>\n{body}\n</requirement_packet>"
    if texts:
        text += "\n\n" + "\n\n".join(texts)
    facts = {
        "approved_documents": approved,
        "recently_reviewed_documents": recent,
        "evidence_items": len(evidence),
        "passing_checks": passing,
        "failing_checks": failing,
    }
    basis_hash = hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return Packet(rid, fw.level, text, refs, facts, attachments, basis_hash, dict(p.reverse))
