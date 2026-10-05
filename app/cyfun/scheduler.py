"""Connector execution: scheduled runs and on-demand runs, persisted as runs, checks and inventory."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from . import db as database
from .config import Settings
from .connectors import registry
from .models import Asset, CheckResult, ConnectorRun, utcnow
from .services import log_activity

log = logging.getLogger("cyfun.scheduler")
_scheduler: BackgroundScheduler | None = None


def run_connector(settings: Settings, key: str, actor: str = "scheduler") -> int:
    """Run one connector synchronously. Returns the run id."""
    connector = registry(settings).get(key)
    if connector is None or not connector.configured():
        raise ValueError(f"connector {key} is not configured")
    db = database.session()
    run = ConnectorRun(connector=key, triggered_by=actor, status="running")
    db.add(run)
    db.commit()
    try:
        result = connector.sync()
        now = utcnow()
        # inventory upsert -------------------------------------------------------------
        existing = {a.external_id: a for a in db.execute(select(Asset).where(Asset.source == key)).scalars().all()}
        seen: set[str] = set()
        for item in result.inventory:
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
        # checks -----------------------------------------------------------------------
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
        # snapshot ---------------------------------------------------------------------
        snap_dir = settings.snapshots_dir / key
        snap_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snap_file = snap_dir / f"{stamp}.json"
        snap_file.write_text(
            json.dumps(
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
            ),
            encoding="utf-8",
        )
        run.snapshot_file = f"{key}/{snap_file.name}"
        run.inventory_count = len(result.inventory)
        run.check_count = len(result.checks)
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


def run_all(settings: Settings, actor: str = "scheduler") -> None:
    for key, connector in registry(settings).items():
        if connector.configured():
            try:
                run_connector(settings, key, actor)
            except Exception:  # noqa: BLE001
                log.exception("connector %s crashed", key)


def start(settings: Settings) -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    hours = max(1, settings.connector_sync_hours)
    sched.add_job(run_all, "interval", hours=hours, args=[settings], id="sync-all", replace_existing=True)
    sched.start()
    _scheduler = sched
    return sched


def trigger(settings: Settings, key: str, actor: str) -> None:
    """Queue an immediate run without blocking the request."""
    if _scheduler is None:
        run_connector(settings, key, actor)
        return
    _scheduler.add_job(run_connector, args=[settings, key, actor], id=f"manual-{key}", replace_existing=True, misfire_grace_time=60)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))
