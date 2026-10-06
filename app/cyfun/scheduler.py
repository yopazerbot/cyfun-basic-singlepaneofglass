"""Background work: connector runs (scheduled and on demand), automated evidence, Claude reviews."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from . import db as database
from .appsettings import load_config
from .config import Settings
from .connectors import registry
from .connectors.base import ERROR, Check, Connector, InventoryItem, SyncResult
from .models import Asset, CheckResult, ConnectorRun, Evidence, utcnow
from .services import log_activity

log = logging.getLogger("cyfun.scheduler")
_scheduler: BackgroundScheduler | None = None
AI_POLL_MINUTES = 5


def run_connector(settings: Settings, key: str, actor: str = "scheduler") -> int:
    """Run one connector synchronously. Returns the run id."""
    connector = registry(load_config(settings=settings)).get(key)
    if connector is None or not connector.configured():
        raise ValueError(f"connector {key} is not configured")
    db = database.session()
    run = ConnectorRun(connector=key, triggered_by=actor, status="running")
    db.add(run)
    db.commit()
    try:
        result = connector.sync()
        now = utcnow()
        _upsert_inventory(db, key, result.inventory, now)
        for ch in result.checks:
            db.add(
                CheckResult(
                    run_id=run.id,
                    connector=key,
                    check_id=ch.id[:60],
                    title=ch.title[:300],
                    status=ch.status,
                    summary=ch.summary[:2000],
                    details=_jsonable(ch.details),
                    requirement_ids=list(ch.requirement_ids),
                    checked_at=now,
                )
            )
        run.snapshot_file, snap_bytes = _write_snapshot(settings, key, result)
        run.inventory_count = len(result.inventory)
        run.check_count = len(result.checks)
        register_evidence(db, connector, run, result.checks, hashlib.sha256(snap_bytes).hexdigest(), len(snap_bytes))
        run.status = "ok"
        run.finished_at = utcnow()
        db.commit()
        log_activity(db, actor, "connector_run", "connector", key, {"run_id": run.id, "checks": run.check_count, "inventory": run.inventory_count})
        log.info("connector %s finished: %s checks, %s inventory items", key, run.check_count, run.inventory_count)
    except Exception as exc:  # noqa: BLE001 - the run record carries the error
        db.rollback()
        run = db.get(ConnectorRun, run.id) or run
        run.status = "error"
        run.error = str(exc)[:2000]
        run.finished_at = utcnow()
        db.add(run)
        db.commit()
        log.exception("connector %s failed", key)
    finally:
        db.close()
    return run.id


def _upsert_inventory(db, key: str, items: list[InventoryItem], now: datetime) -> None:
    """Refresh this connector's assets; items no longer reported are retired, returning ones reactivated."""
    existing = {a.external_id: a for a in db.execute(select(Asset).where(Asset.source == key)).scalars().all()}
    seen: set[str] = set()
    for item in items:
        seen.add(item.external_id)
        a = existing.get(item.external_id)
        if a is None:
            a = Asset(kind=item.kind, name=item.name[:300], source=key, external_id=item.external_id[:300])
            db.add(a)
        a.name = item.name[:300] or a.name
        a.description = item.description[:2000]
        a.location = item.location[:200]
        a.attributes = item.attributes
        a.last_seen_at = now
        if a.lifecycle == "retired":
            a.lifecycle = "active"
    for ext, a in existing.items():
        if ext not in seen and a.lifecycle != "retired":
            a.lifecycle = "retired"
            a.attributes = {**(a.attributes or {}), "missing_since": now.date().isoformat()}


def _write_snapshot(settings: Settings, key: str, result: SyncResult) -> tuple[str, bytes]:
    """Write the run's JSON snapshot. Returns its path relative to the snapshots folder and its bytes."""
    snap_dir = settings.snapshots_dir / key
    snap_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snap_bytes = json.dumps(
        {
            "connector": key,
            "collected_at": stamp,
            "checks": [
                {
                    "id": c.id,
                    "title": c.title,
                    "status": c.status,
                    "summary": c.summary,
                    "requirements": c.requirement_ids,
                    "details": _jsonable(c.details),
                }
                for c in result.checks
            ],
            "inventory_count": len(result.inventory),
            "raw": _jsonable(result.raw),
        },
        indent=1,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    (snap_dir / f"{stamp}.json").write_bytes(snap_bytes)
    return f"{key}/{stamp}.json", snap_bytes


def register_evidence(db, connector: Connector, run: ConnectorRun, checks: list[Check], digest: str, size: int) -> None:
    """Keep one automated evidence item per check, refreshed by every successful run.

    The item points at the run's snapshot (SHA-256 of the JSON file) and carries the check's
    requirement mapping. Requirements an administrator added to the item are kept. A check that
    ends in an error leaves its item as it was, with the date of the last real result."""
    existing = {e.source_ref: e for e in db.execute(select(Evidence).where(Evidence.kind == "automated", Evidence.source == connector.key)).scalars().all()}
    for ch in checks:
        if ch.status == ERROR:
            continue
        ev = existing.get(ch.id[:60])
        if ev is None:
            ev = Evidence(kind="automated", source=connector.key, source_ref=ch.id[:60], requirement_ids=[], collected_by=f"connector {connector.name}")
            db.add(ev)
        ev.requirement_ids = sorted(set(ev.requirement_ids or []) | set(ch.requirement_ids))
        ev.title = f"{connector.name}: {ch.title}"[:300]
        ev.description = f"{ch.status.upper()}: {ch.summary}"[:2000]
        ev.run_id = run.id
        ev.url = f"/connectors/{connector.key}/snapshot/{run.id}"
        ev.file_name = run.snapshot_file.rsplit("/", 1)[-1]
        ev.mime = "application/json"
        ev.sha256 = digest
        ev.size = size
        ev.collected_on = utcnow().date()


def run_all(settings: Settings, actor: str = "scheduler") -> None:
    config = load_config(settings=settings)
    keys = [key for key, connector in registry(config).items() if connector.configured()]
    for key in keys:
        try:
            run_connector(settings, key, actor)
        except Exception:  # noqa: BLE001
            log.exception("connector %s crashed", key)
    if keys and actor == "scheduler" and config.ai_review_after_sync:
        from .ai import service as ai

        db = database.session()
        try:
            ai.submit_batch(db, settings, scope="changed", actor="scheduler")
        except ai.AiError as exc:
            log_activity(db, "scheduler", "ai_batch_skipped", "ai", "", {"reason": str(exc)})
        except Exception:  # noqa: BLE001
            log.exception("review after sync failed")
        finally:
            db.close()


def poll_ai_batches(settings: Settings) -> None:
    from .ai import service as ai

    try:
        ai.poll_batches(settings)
    except Exception:  # noqa: BLE001
        log.exception("polling Claude batches failed")


def start(settings: Settings) -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    hours = max(1, int(load_config(settings=settings).connector_sync_hours))
    sched.add_job(run_all, "interval", hours=hours, args=[settings], id="sync-all", replace_existing=True)
    sched.add_job(poll_ai_batches, "interval", minutes=AI_POLL_MINUTES, args=[settings], id="ai-batches", replace_existing=True)
    sched.start()
    _scheduler = sched
    return sched


def reschedule(hours: int) -> None:
    """Apply a new connector interval without a restart."""
    if _scheduler is not None and _scheduler.get_job("sync-all") is not None:
        _scheduler.reschedule_job("sync-all", trigger="interval", hours=max(1, int(hours)))


def submit(func, *args, job_id: str) -> None:
    """Run work in the background thread pool, or inline when the scheduler is off (tests, one-off scripts)."""
    if _scheduler is None:
        func(*args)
        return
    _scheduler.add_job(func, args=list(args), id=job_id, replace_existing=True, misfire_grace_time=60)


def trigger(settings: Settings, key: str, actor: str) -> None:
    """Queue an immediate connector run without blocking the request."""
    submit(run_connector, settings, key, actor, job_id=f"manual-{key}")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))
