"""
Source orchestrator (Amendment §1). Routes a source job to its connector, records a scraper_jobs
row, and enforces the state machine: HALTED/DISABLED/PAUSED sources are never run; login-session
sources run only on the `login_session` queue.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from celery import shared_task
from sqlalchemy import select

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.harvest_mode import (
    backfill_progress,
    cadence_for_source,
    get_harvest_mode,
    selected_source_names,
    set_harvest_mode,
    source_backfill_priority,
    source_selected_for_mode,
)
from scraper.models import ScraperJob, ScraperSource
from scraper.notify import notify

logger = logging.getLogger(__name__)


async def _connector_for(source: ScraperSource):
    name = source.source_name
    if source.access_method == "login_session":
        from scraper.tasks.pakistanlawsite import scrape_pakistanlawsite

        return scrape_pakistanlawsite
    if name == "NasirLawSite":
        from scraper.tasks.nasirlawsite import scrape_nasirlawsite

        return scrape_nasirlawsite
    if name == "PakistanCode":
        from scraper.tasks.pakistancode import scrape_pakistancode

        return scrape_pakistancode
    from scraper.tasks.legislatures import LEGISLATURE_SOURCES, scrape_legislature
    from scraper.tasks.superior_courts import COURT_SOURCES, scrape_superior_court

    if name in COURT_SOURCES:
        return scrape_superior_court
    if name in LEGISLATURE_SOURCES:
        return scrape_legislature
    raise LookupError(f"no connector for source {name}")


async def run_source(source_name: str, **connector_kwargs) -> Dict[str, Any]:
    async with SessionLocal() as db:
        source = (
            await db.execute(
                select(ScraperSource).where(ScraperSource.source_name == source_name).with_for_update()
            )
        ).scalars().first()
        if source is None:
            return {"error": f"source {source_name} not found"}
        if not source.is_active or source.state in ("HALTED", "DISABLED"):
            logger.info("%s is %s; not dispatched (%s)", source_name, source.state, source.state_reason)
            return {"skipped": source.state, "reason": source.state_reason}
        if source.access_method == "login_session":
            if not settings.login_scraping_effective:
                await notify(db, level="warning", code="LOGIN_SCRAPING_DISABLED", message="ALLOW_LOGIN_SCRAPING is false or ENVIRONMENT != chambers; PakistanLawSite not run", source_name=source_name)
                await db.commit()
                return {"skipped": "login_scraping_disabled"}
            if source.state == "PAUSED":
                return {"skipped": "PAUSED", "reason": source.state_reason}
        elif source.state == "PAUSED":
            return {"skipped": "PAUSED", "reason": source.state_reason}
        existing = (
            await db.execute(
                select(ScraperJob).where(
                    ScraperJob.source_name == source_name,
                    ScraperJob.status == "running",
                )
            )
        ).scalars().first()
        if existing is not None:
            logger.info(
                "%s already has running job %s; skipping duplicate kick",
                source_name,
                existing.id,
            )
            return {"skipped": "already_running", "job_id": str(existing.id)}
        job = ScraperJob(source_id=source.id, source_name=source_name, job_type="scrape", status="running", worker_hostname=socket.gethostname(), started_at=datetime.now(timezone.utc))
        db.add(job)
        await db.commit()
        try:
            connector = await _connector_for(source)
            stats = await connector(source, db, job_id=job.id, **connector_kwargs)
            job.status = "done"
            job.result_summary = stats
            job.pages_scraped = int(stats.get("pages", stats.get("fetched", 0)) or 0)
            job.records_extracted = int(stats.get("staged", 0) or 0)
            job.records_quarantined = int(stats.get("quarantined", 0) or 0)
        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)[:4000]
            source.last_error = str(exc)[:2000]
            logger.exception("source job failed: %s", source_name)
            stats = {"error": str(exc)}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return stats


@shared_task(name="scraper.tasks.dispatcher.run_source_job", bind=True, max_retries=0)
def run_source_job(self, source_name: str):
    return run_async(run_source(source_name))


@shared_task(name="scraper.tasks.dispatcher.run_login_session_job", bind=True, max_retries=0)
def run_login_session_job(self, source_name: str = "PakistanLawSite"):
    """Separate task name so Celery routes it to the single-concurrency login_session queue."""
    return run_async(run_source(source_name))


async def dispatch_due_sources() -> Dict[str, Any]:
    """Beat entry: enqueue every ACTIVE source whose schedule is due."""
    from scraper.tasks.celery_app import app

    now = datetime.now(timezone.utc)
    queued = []
    mode = settings.HARVEST_MODE
    auto_switched = False
    async with SessionLocal() as db:
        mode = await get_harvest_mode(db)
        if mode == "backfill" and settings.HARVEST_AUTO_SWITCH:
            selected = await selected_source_names(db, "backfill")
            progress = await backfill_progress(db, source_names=selected)
            if progress["complete"]:
                await set_harvest_mode(
                    db,
                    "updates",
                    changed_by="system",
                    reason=(
                        "auto-switch: frontier drained"
                        f", judgments={progress['judgments_total']}, statutes={progress['statutes_total']}"
                    ),
                )
                await notify(
                    db,
                    level="info",
                    code="HARVEST_MODE_SWITCHED",
                    message="Backfill completion criteria met; switched to updates cadence.",
                    source_name=None,
                    details=progress,
                )
                mode = "updates"
                auto_switched = True
        rows = (
            await db.execute(
                select(ScraperSource).where(ScraperSource.is_active.is_(True), ScraperSource.state == "ACTIVE")
            )
        ).scalars().all()
        if mode == "backfill":
            rows.sort(key=source_backfill_priority)
        for s in rows:
            if not source_selected_for_mode(s, mode):
                continue
            due = s.next_scrape_at is None or s.next_scrape_at <= now
            if not due:
                continue
            running = (
                await db.execute(
                    select(ScraperJob.id).where(
                        ScraperJob.source_name == s.source_name,
                        ScraperJob.status == "running",
                    )
                )
            ).scalars().first()
            if running is not None:
                logger.info(
                    "%s due but job %s still running; not re-queued",
                    s.source_name,
                    running,
                )
                continue
            if s.access_method == "login_session":
                app.send_task("scraper.tasks.dispatcher.run_login_session_job", args=(s.source_name,), queue="login_session")
            else:
                app.send_task("scraper.tasks.dispatcher.run_source_job", args=(s.source_name,), queue="scraper")
            s.next_scrape_at = now + timedelta(minutes=cadence_for_source(s, mode))
            queued.append(s.source_name)
        await db.commit()
    return {"mode": mode, "auto_switched": auto_switched, "queued": queued}


@shared_task(name="scraper.tasks.dispatcher.dispatch_due_sources")
def dispatch_due_sources_task():
    return run_async(dispatch_due_sources())
