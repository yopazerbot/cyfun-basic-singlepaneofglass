"""Proposal workflow: start a review, run it, batch reviews, spend limit, accept or reject.

A proposal never changes a score by itself. `accept` is the only path from a proposal to the
Score table, it runs on an administrator's request, and it records the proposal, the model,
the prompt version and the input hash in the activity log.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import db as database
from ..appsettings import AI_MODELS, load_config
from ..config import Settings
from ..framework import Framework
from ..models import Action, AiBatch, AiProposal, Evidence, Score, utcnow
from ..services import current_framework, log_activity, parse_int
from ..views import fmt_usd
from . import claude, guard
from .packet import Packet, build_packet, evidence_file, pseudonymizer_for
from .prompt import PROMPT_VERSION, system_prompt, user_instruction
from .pseudonym import Pseudonymizer

log = logging.getLogger("cyfun.ai")

ACTIVE = ("queued", "running")
STALE_MINUTES = 20


class AiError(Exception):
    """A reason shown to the administrator."""


# --------------------------------------------------------------------------- status and spend
def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend(db: Session) -> float:
    total = db.execute(select(func.coalesce(func.sum(AiProposal.cost_usd), 0.0)).where(AiProposal.created_at >= month_start())).scalar_one()
    return float(total)


def status(db: Session, settings: Settings) -> dict:
    cfg = load_config(db, settings)
    return {
        "configured": bool(cfg.anthropic_api_key),
        "key_source": cfg.sources["anthropic_api_key"],
        "key_problem": cfg.problems.get("anthropic_api_key", ""),
        "model": cfg.ai_model,
        "model_label": AI_MODELS.get(cfg.ai_model, cfg.ai_model),
        "effort": cfg.ai_effort,
        "cap": cfg.ai_monthly_cap_usd,
        "spend": month_spend(db),
        "after_sync": cfg.ai_review_after_sync,
        "single_estimate": claude.estimate(cfg.ai_model, cfg.ai_effort, 9000),
    }


def _ensure_ready(db: Session, cfg, needed: float) -> None:
    if not cfg.anthropic_api_key:
        raise AiError("No Anthropic API key is set. Add one on the Settings page.")
    cap = int(cfg.ai_monthly_cap_usd)
    if cap <= 0:
        raise AiError("Reviews are off: the monthly spend limit on the Settings page is 0.")
    spent = month_spend(db)
    if spent + needed > cap:
        raise AiError(f"This would pass the monthly spend limit of USD {cap}: {fmt_usd(spent)} spent this month, about {fmt_usd(needed)} needed.")


def _estimate(packet: Packet, cfg, batch: bool) -> float:
    images = sum(1 for a in packet.attachments if a["type"] == "image")
    binary = sum(int(a.get("size", 0)) for a in packet.attachments if a["type"] != "image")
    return claude.estimate(cfg.ai_model, cfg.ai_effort, len(packet.text) + len(system_prompt()), binary, images, batch)


def recover_stale(db: Session, minutes: int = STALE_MINUTES) -> int:
    """Running reviews older than `minutes` were interrupted; at start-up every running review was (minutes=0)."""
    cutoff = utcnow() - timedelta(minutes=minutes)
    rows = db.execute(select(AiProposal).where(AiProposal.status == "running", AiProposal.created_at < cutoff)).scalars().all()
    for p in rows:
        p.status = "failed"
        p.error = "The review was interrupted, probably by a restart of the application. Start it again."
        p.finished_at = utcnow()
    if rows:
        db.commit()
    return len(rows)


# --------------------------------------------------------------------------- building requests
def _input_hash(packet: Packet) -> str:
    h = hashlib.sha256()
    for part in (PROMPT_VERSION, system_prompt(), packet.text, *[a.get("sha256", "") for a in packet.attachments]):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _new_proposal(packet: Packet, cfg, origin: str, actor: str, state: str, batch_id: int | None = None) -> AiProposal:
    return AiProposal(
        requirement_id=packet.rid,
        level=packet.level,
        status=state,
        origin=origin,
        batch_id=batch_id,
        model=cfg.ai_model,
        effort=cfg.ai_effort,
        prompt_version=PROMPT_VERSION,
        input_text=packet.text,
        input_sha256=_input_hash(packet),
        basis_hash=packet.basis_hash,
        pseudonyms=packet.pseudonyms,
        meta=packet.meta,
        created_by=actor,
    )


def _content(db: Session, settings: Settings, prop: AiProposal) -> list[dict]:
    """Message content: shared PDF and image evidence first, then the instruction and the packet."""
    blocks: list[dict] = []
    missing: list[str] = []
    for att in (prop.meta or {}).get("attachments", []):
        path = evidence_file(settings, db.get(Evidence, att.get("evidence_id")))
        if path is None:
            missing.append(att["ref"])
            continue
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        blocks.append({"type": "text", "text": f"Content of evidence {att['ref']}:"})
        if att["type"] == "pdf":
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}, "title": f"Evidence {att['ref']}"})
        else:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": att["media_type"], "data": data}})
    text = user_instruction(prop.requirement_id, prop.level) + "\n\n" + prop.input_text
    if missing:
        text += "\n\nNote: the content of " + ", ".join(missing) + " could not be attached (file missing)."
    blocks.append({"type": "text", "text": text})
    return blocks


# --------------------------------------------------------------------------- single review
def start_review(db: Session, settings: Settings, rid: str, actor: str) -> AiProposal:
    fw = current_framework(db)
    if fw.get(rid) is None:
        raise AiError(f"{rid} is not part of the {fw.level} requirement set.")
    recover_stale(db)
    busy = db.execute(select(AiProposal.id).where(AiProposal.requirement_id == rid, AiProposal.status.in_(ACTIVE))).first()
    if busy:
        raise AiError("A review of this requirement is already in progress.")
    cfg = load_config(db, settings)
    packet = build_packet(db, settings, fw, rid)
    _ensure_ready(db, cfg, _estimate(packet, cfg, batch=False))
    prop = _new_proposal(packet, cfg, "single", actor, "running")
    db.add(prop)
    db.commit()
    log_activity(db, actor, "ai_review_start", "score", rid, {"proposal": prop.id, "model": cfg.ai_model, "effort": cfg.ai_effort})
    return prop


def run_review(settings: Settings, proposal_id: int) -> None:
    """Execute a started review. Runs in the background thread pool."""
    db = database.session()
    try:
        prop = db.get(AiProposal, proposal_id)
        if prop is None or prop.status != "running":
            return
        cfg = load_config(db, settings)
        if not cfg.anthropic_api_key:
            _fail(db, prop, "No Anthropic API key is set.")
            return
        body = claude.params(prop.model, prop.effort, system_prompt(), _content(db, settings, prop))
        try:
            reply = claude.review(claude.client(cfg.anthropic_api_key), body)
        except claude.ApiError as exc:
            _fail(db, prop, str(exc))
            return
        apply_reply(db, prop, reply, batch=False)
    except Exception as exc:  # noqa: BLE001 - the proposal carries the error
        log.exception("review %s failed", proposal_id)
        db.rollback()
        prop = db.get(AiProposal, proposal_id)
        if prop is not None and prop.status in ACTIVE:
            _fail(db, prop, f"Internal error: {type(exc).__name__}")
    finally:
        db.close()


def _fail(db: Session, prop: AiProposal, message: str) -> None:
    prop.status = "failed"
    prop.error = message[:2000]
    prop.finished_at = utcnow()
    db.commit()
    log_activity(db, "Claude", "ai_review_failed", "score", prop.requirement_id, {"proposal": prop.id, "error": prop.error[:300]})


def apply_reply(db: Session, prop: AiProposal, reply: claude.Reply, batch: bool) -> None:
    prop.served_model = reply.served_model or prop.model
    prop.request_id = reply.request_id[:100]
    prop.input_tokens, prop.output_tokens = reply.usage.input, reply.usage.output
    prop.cache_read_tokens, prop.cache_write_tokens = reply.usage.cache_read, reply.usage.cache_write
    prop.cost_usd = claude.cost(prop.model, prop.served_model, reply.usage, batch)
    prop.raw_output = reply.raw_text[:200_000]
    prop.finished_at = utcnow()
    if reply.stop_reason == "refusal":
        prop.status = "declined"
        prop.error = f"Claude declined to review this requirement (safety category: {reply.refusal}). Score it manually."
    elif reply.stop_reason == "max_tokens":
        prop.status = "failed"
        prop.error = "The answer was cut off at the output limit. Try again, or lower the reasoning effort on the Settings page."
    elif reply.data is None:
        prop.status = "failed"
        prop.error = "The answer was not valid JSON."
    else:
        try:
            checked = guard.check(reply.data, prop.meta.get("refs", {}), prop.meta.get("facts", {}), Pseudonymizer.from_mapping(prop.pseudonyms or {}))
        except ValidationError as exc:
            prop.status = "failed"
            prop.error = f"The answer did not match the expected structure ({exc.error_count()} problems)."
        else:
            prop.doc_score, prop.impl_score = checked.doc, checked.impl
            prop.confidence, prop.justification, prop.result = checked.confidence, checked.justification, checked.result
            prop.status = "ready"
            for old in db.execute(
                select(AiProposal).where(AiProposal.requirement_id == prop.requirement_id, AiProposal.status == "ready", AiProposal.id != prop.id)
            ).scalars():
                old.status = "superseded"
    db.commit()
    log_activity(
        db,
        "Claude",
        "ai_review_done",
        "score",
        prop.requirement_id,
        {
            "proposal": prop.id,
            "status": prop.status,
            "documentation": prop.doc_score,
            "implementation": prop.impl_score,
            "model": prop.served_model,
            "cost_usd": round(prop.cost_usd, 4),
        },
    )


# --------------------------------------------------------------------------- batch review
def _latest_basis(db: Session) -> dict[str, str]:
    """Requirement id -> basis hash of its latest review that produced a result."""
    rows = db.execute(
        select(AiProposal.requirement_id, AiProposal.basis_hash).where(AiProposal.status.notin_(["failed", *ACTIVE])).order_by(AiProposal.id)
    ).all()
    return dict(rows)  # ordered by id, so the latest review wins


def batch_candidates(db: Session, settings: Settings, fw: Framework, scope: str) -> list[Packet]:
    """Packets for the requirements a batch would review.

    "all": every requirement of the level without a review in progress.
    "changed": only those whose material changed since their latest finished review (or never reviewed)."""
    active = set(db.execute(select(AiProposal.requirement_id).where(AiProposal.status.in_(ACTIVE))).scalars().all())
    latest = _latest_basis(db)
    pseudo = pseudonymizer_for(db)
    out = []
    for req in fw.requirements:
        if req.id in active:
            continue
        packet = build_packet(db, settings, fw, req.id, pseudo=pseudo)
        if scope == "changed" and latest.get(req.id) == packet.basis_hash:
            continue
        out.append(packet)
    return out


def batch_preview(db: Session, settings: Settings, fw: Framework) -> dict:
    """Counts and estimates for the two batch scopes, shown before submitting."""
    cfg = load_config(db, settings)
    packets = batch_candidates(db, settings, fw, "all")
    latest = _latest_basis(db)
    changed = [p for p in packets if latest.get(p.rid) != p.basis_hash]
    return {
        "all": len(packets),
        "all_usd": sum(_estimate(p, cfg, batch=True) for p in packets),
        "changed": len(changed),
        "changed_usd": sum(_estimate(p, cfg, batch=True) for p in changed),
    }


def submit_batch(db: Session, settings: Settings, scope: str, actor: str) -> AiBatch:
    if scope not in ("changed", "all"):
        raise AiError("Unknown batch scope.")
    if db.execute(select(AiBatch.id).where(AiBatch.status == "submitted")).first():
        raise AiError("A batch is still being processed. Wait for its results or cancel it.")
    cfg = load_config(db, settings)
    if not cfg.anthropic_api_key:
        raise AiError("No Anthropic API key is set. Add one on the Settings page.")
    fw = current_framework(db)
    packets = batch_candidates(db, settings, fw, scope)
    if not packets:
        raise AiError("Nothing to review: no requirement changed since its last review." if scope == "changed" else "Nothing to review.")
    estimate = sum(_estimate(p, cfg, batch=True) for p in packets)
    _ensure_ready(db, cfg, estimate)
    batch = AiBatch(scope=scope, level=fw.level, model=cfg.ai_model, request_count=len(packets), estimate_usd=estimate, created_by=actor)
    db.add(batch)
    db.flush()
    props = [_new_proposal(p, cfg, "batch", actor, "queued", batch.id) for p in packets]
    db.add_all(props)
    db.flush()
    entries = [(f"p{prop.id}", claude.params(prop.model, prop.effort, system_prompt(), _content(db, settings, prop))) for prop in props]
    try:
        batch.anthropic_id = claude.submit_batch(claude.client(cfg.anthropic_api_key), entries)
    except claude.ApiError as exc:
        db.rollback()
        raise AiError(str(exc)) from exc
    db.commit()
    log_activity(
        db, actor, "ai_batch_submit", "ai", str(batch.id), {"scope": scope, "requests": len(props), "level": fw.level, "estimate_usd": round(estimate, 2)}
    )
    return batch


_RESULT_ERRORS = {
    "errored": "The request failed in the batch",
    "expired": "The batch expired before this request ran (24 hours).",
    "canceled": "The batch was canceled before this request ran.",
}


def poll_batches(settings: Settings, only_id: int | None = None) -> int:
    """Collect results of finished batches. Returns the number of batches that ended."""
    db = database.session()
    ended = 0
    try:
        query = select(AiBatch).where(AiBatch.status == "submitted")
        if only_id is not None:
            query = query.where(AiBatch.id == only_id)
        batches = db.execute(query).scalars().all()
        if not batches:
            return 0
        cfg = load_config(db, settings)
        if not cfg.anthropic_api_key:
            return 0
        c = claude.client(cfg.anthropic_api_key)
        for batch in batches:
            batch.checked_at = utcnow()
            try:
                state = claude.batch_status(c, batch.anthropic_id)
                if state != "ended":
                    db.commit()
                    continue
                seen: set[int] = set()
                for res in claude.batch_results(c, batch.anthropic_id):
                    try:
                        pid = int(str(res.custom_id).lstrip("p"))
                    except ValueError:
                        continue
                    prop = db.get(AiProposal, pid)
                    if prop is None or prop.batch_id != batch.id or prop.status != "queued":
                        continue
                    seen.add(pid)
                    kind = res.result.type
                    if kind == "succeeded":
                        apply_reply(db, prop, claude.reply_from(res.result.message), batch=True)
                    else:
                        detail = ""
                        if kind == "errored":
                            err = getattr(getattr(res.result, "error", None), "error", None)
                            detail = f": {getattr(err, 'type', '')} {getattr(err, 'message', '')}".rstrip(": ")
                        _fail(db, prop, _RESULT_ERRORS.get(kind, "No result") + detail)
            except claude.ApiError as exc:
                batch.error = str(exc)
                db.commit()
                continue
            for prop in db.execute(select(AiProposal).where(AiProposal.batch_id == batch.id, AiProposal.status == "queued")).scalars():
                _fail(db, prop, "The batch ended without a result for this requirement.")
            props = db.execute(select(AiProposal).where(AiProposal.batch_id == batch.id)).scalars().all()
            batch.succeeded = sum(1 for p in props if p.status not in ("failed", *ACTIVE))
            batch.failed = sum(1 for p in props if p.status == "failed")
            batch.status = "ended"
            batch.ended_at = utcnow()
            db.commit()
            ended += 1
            log_activity(db, "Claude", "ai_batch_done", "ai", str(batch.id), {"succeeded": batch.succeeded, "failed": batch.failed})
        return ended
    finally:
        db.close()


def cancel_batch(db: Session, settings: Settings, batch: AiBatch, actor: str) -> None:
    if batch.status != "submitted":
        raise AiError("This batch is no longer running.")
    cfg = load_config(db, settings)
    if not cfg.anthropic_api_key:
        raise AiError("No Anthropic API key is set.")
    try:
        claude.cancel_batch(claude.client(cfg.anthropic_api_key), batch.anthropic_id)
    except claude.ApiError as exc:
        raise AiError(str(exc)) from exc
    batch.error = f"Cancel requested by {actor}."
    db.commit()
    log_activity(db, actor, "ai_batch_cancel", "ai", str(batch.id), {})


# --------------------------------------------------------------------------- decisions
def accept(
    db: Session, fw: Framework, prop: AiProposal, doc_raw: str, impl_raw: str, justification: str, action_indexes: list[int], actor: str
) -> list[Action]:
    if prop.status != "ready":
        raise AiError("This proposal is no longer open.")
    rid = prop.requirement_id
    if fw.get(rid) is None:
        raise AiError(f"{rid} is not part of the {fw.level} requirement set.")
    doc = parse_int(doc_raw, 1, 5)
    impl = parse_int(impl_raw, 1, 5)
    if doc is None or impl is None:
        raise AiError("Choose a documentation and an implementation score from 1 to 5.")
    justification = (justification or "").strip()
    score = db.get(Score, rid)
    if score is None:
        score = Score(requirement_id=rid)
        db.add(score)
    before = {"doc": score.doc_score, "impl": score.impl_score, "na": score.not_applicable}
    edited = doc != prop.doc_score or impl != prop.impl_score or justification != (prop.justification or "").strip()
    score.doc_score, score.impl_score, score.not_applicable = doc, impl, False
    score.justification = justification
    score.updated_by = actor
    score.ai_proposal_id = prop.id
    prop.status = "accepted"
    prop.decision = "edited" if edited else "accepted"
    prop.decided_by = actor
    prop.decided_at = utcnow()
    proposed = (prop.result or {}).get("proposed_actions", [])
    created = []
    for i in sorted(set(action_indexes)):
        if 0 <= i < len(proposed):
            a = Action(title=str(proposed[i].get("title", ""))[:300] or "Action", requirement_id=rid, description=str(proposed[i].get("reason", ""))[:2000])
            db.add(a)
            created.append(a)
    db.commit()
    after = {"doc": doc, "impl": impl, "na": False}
    log_activity(
        db,
        actor,
        "score_update",
        "score",
        rid,
        {
            "before": before,
            "after": after,
            "level": fw.level,
            "ai": {
                "proposal": prop.id,
                "model": prop.served_model or prop.model,
                "prompt_version": prop.prompt_version,
                "input_sha256": prop.input_sha256,
                "edited": edited,
            },
        },
    )
    for a in created:
        log_activity(db, actor, "action_create", "action", str(a.id), {"title": a.title, "requirement": rid, "from_proposal": prop.id})
    return created


def reject(db: Session, prop: AiProposal, note: str, actor: str) -> None:
    if prop.status != "ready":
        raise AiError("This proposal is no longer open.")
    prop.status = "rejected"
    prop.decision = "rejected"
    prop.decision_note = (note or "").strip()[:1000]
    prop.decided_by = actor
    prop.decided_at = utcnow()
    db.commit()
    log_activity(db, actor, "ai_proposal_reject", "score", prop.requirement_id, {"proposal": prop.id, "note": prop.decision_note[:300]})


def latest_for(db: Session, rid: str) -> AiProposal | None:
    """The proposal to show on a requirement page: the newest one that still needs attention."""
    p = db.execute(select(AiProposal).where(AiProposal.requirement_id == rid).order_by(AiProposal.id.desc()).limit(1)).scalar_one_or_none()
    return p if p is not None and p.status in ("queued", "running", "ready", "failed", "declined") else None
