"""
Source orchestrator (Amendment §1). Routes a source job to its connector, records a scraper_jobs
row, and enforces the state machine: HALTED/DISABLED/PAUSED sources are never run; login-session
sources run only on the `login_session` queue.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from celery import shared_task
from sqlalchemy import select

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.auth.session_manager import SessionLockHeld
from scraper.models import ScraperJob, ScraperSource
from scraper.notify import notify

logger = logging.getLogger(__name__)
RUNNING_JOB_STALE_AFTER = timedelta(hours=3)
# A live connector heartbeats its scraper_jobs row (updated_at) on every page it charges and on
# every committed progress step. A running row that has not been touched for this long belongs to
# a worker that was recreated (deploy, OOM, restart) and would otherwise block its source for
# RUNNING_JOB_STALE_AFTER.
RUNNING_JOB_HEARTBEAT_STALE_AFTER = timedelta(minutes=30)


def _running_started_at(job: ScraperJob) -> Optional[datetime]:
    return job.started_at or job.created_at


def _running_heartbeat_at(job: ScraperJob) -> Optional[datetime]:
    candidates = [t for t in (job.updated_at, job.started_at, job.created_at) if t is not None]
    return max(candidates) if candidates else None


async def _active_running_job(db, source_name: str, *, now: datetime) -> tuple[Optional[ScraperJob], bool]:
    """Return the newest live running job while retiring stale/zombie running rows. One job per
    source runs at a time."""
    running_jobs = (
        await db.execute(
            select(ScraperJob).where(
                ScraperJob.source_name == source_name,
                ScraperJob.status == "running",
            )
        )
    ).scalars().all()
    if not running_jobs:
        return None, False
    stale_cutoff = now - RUNNING_JOB_STALE_AFTER
    heartbeat_cutoff = now - RUNNING_JOB_HEARTBEAT_STALE_AFTER
    running_jobs.sort(
        key=lambda job: _running_started_at(job) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    active_job: Optional[ScraperJob] = None
    mutated = False
    for job in running_jobs:
        started_at = _running_started_at(job)
        heartbeat_at = _running_heartbeat_at(job)
        stale = started_at is None or started_at <= stale_cutoff
        if not stale and heartbeat_at is not None and heartbeat_at <= heartbeat_cutoff:
            stale = True
        if active_job is not None:
            stale = True
        if not stale:
            active_job = job
            continue
        if job.status != "failed":
            job.status = "failed"
            mutated = True
        if job.finished_at is None:
            job.finished_at = now
            mutated = True
        if not job.error_message:
            if started_at is None:
                reason = "dispatcher marked stale running job as failed (missing started_at)."
            elif heartbeat_at is not None and heartbeat_at <= heartbeat_cutoff and started_at > stale_cutoff:
                silent_seconds = int(max(0, (now - heartbeat_at).total_seconds()))
                reason = f"dispatcher marked running job as failed: no heartbeat for {silent_seconds}s (worker restarted?)."
            else:
                age_seconds = int(max(0, (now - started_at).total_seconds()))
                reason = f"dispatcher marked stale running job as failed (age_seconds={age_seconds})."
            job.error_message = reason
            mutated = True
        logger.warning(
            "Recovered stale running job source=%s job_id=%s started_at=%s active_job=%s",
            source_name,
            job.id,
            started_at,
            active_job.id if active_job is not None else None,
        )
    return active_job, mutated


async def retire_orphaned_login_jobs(*, reason: str = "login-session worker started", redis_client=None) -> int:
    """Called when the login-session worker boots. Every login-session job still recorded as
    running belonged to the previous worker process (a deploy or crash ended it mid-run), because
    only this worker runs that queue; retire the rows now instead of leaving the source blocked for
    RUNNING_JOB_HEARTBEAT_STALE_AFTER, and drop the Redis session lock those jobs held (it expires
    only after LOCK_TTL_SECONDS, an hour, during which every new job skips as "lock held"). The
    frontier cursor is flushed per detail page, so the next job resumes where the dead one stopped."""
    from scraper.auth.session_manager import LOCK_KEY

    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ScraperJob)
                .join(ScraperSource, ScraperSource.id == ScraperJob.source_id)
                .where(ScraperSource.access_method == "login_session", ScraperJob.status == "running")
            )
        ).scalars().all()
        for job in rows:
            job.status = "failed"
            job.finished_at = now
            job.error_message = f"{reason}: this job's process is gone (recorded running since {_running_started_at(job)}); retired at worker start."
            logger.warning("Retired orphaned login-session job source=%s job_id=%s", job.source_name, job.id)
        await db.commit()
        source_names = (
            await db.execute(select(ScraperSource.source_name).where(ScraperSource.access_method == "login_session"))
        ).scalars().all()
    # The locks of the dead processes: only this worker takes them, so none can be live now.
    keys = [LOCK_KEY.format(source=name) for name in source_names]
    if keys:
        own_client = redis_client is None
        if own_client:
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(settings.REDIS_URL)
        try:
            dropped = int(await redis_client.delete(*keys) or 0)
            if dropped:
                logger.warning("Dropped %s stale login-session lock key(s) at worker start", dropped)
        finally:
            if own_client:
                try:
                    await redis_client.aclose()
                except Exception:
                    pass
    return len(rows)


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
        existing_job, recovered_stale = await _active_running_job(db, source_name, now=datetime.now(timezone.utc))
        if existing_job is not None:
            if recovered_stale:
                await db.commit()
            logger.info("%s already has a running job; skipping duplicate kick", source_name)
            return {"skipped": "already_running", "job_id": str(existing_job.id)}
        job = ScraperJob(source_id=source.id, source_name=source_name, job_type="scrape", status="running", worker_hostname=socket.gethostname(), started_at=datetime.now(timezone.utc))
        db.add(job)
        await db.commit()
        try:
            connector = await _connector_for(source)
            stats = await connector(source, db, job_id=job.id, **connector_kwargs)
            job.status = "done"
            job.result_summary = stats
            job.pages_scraped = int(stats.get("pages_charged") or stats.get("pages") or stats.get("fetched") or 0)
            job.records_extracted = int(stats.get("staged", 0) or 0)
            job.records_quarantined = int(stats.get("quarantined", 0) or 0)
        except SessionLockHeld:
            job.status = "done"
            job.error_message = None
            stats = {"skipped": "login_session_lock_held", "pages": 0, "staged": 0}
            job.result_summary = stats
            logger.info("%s already has an active login-session worker; skipped duplicate job", source_name)
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
    """Login-session queue: one worker, one job at a time (specification 3.1)."""
    return run_async(run_source(source_name))


async def dispatch_due_sources() -> Dict[str, Any]:
    """Beat entry (specification 10): enqueue every ACTIVE source whose next_scrape_at is due, then
    set next_scrape_at = now + scrape_frequency_hours."""
    from scraper.tasks.celery_app import app

    now = datetime.now(timezone.utc)
    queued = []
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ScraperSource).where(ScraperSource.is_active.is_(True), ScraperSource.state == "ACTIVE")
            )
        ).scalars().all()
        for s in rows:
            due = s.next_scrape_at is None or s.next_scrape_at <= now
            if not due:
                continue
            running_job, _ = await _active_running_job(db, s.source_name, now=now)
            if running_job is not None:
                logger.info("skip enqueue %s: a scrape job is already running", s.source_name)
                continue
            if s.access_method == "login_session":
                app.send_task("scraper.tasks.dispatcher.run_login_session_job", args=(s.source_name,), queue="login_session")
            else:
                app.send_task("scraper.tasks.dispatcher.run_source_job", args=(s.source_name,), queue="scraper")
            queued.append(s.source_name)
            s.next_scrape_at = now + timedelta(hours=max(1, int(s.scrape_frequency_hours or 24)))
        await db.commit()
    return {"queued": queued}


@shared_task(name="scraper.tasks.dispatcher.dispatch_due_sources")
def dispatch_due_sources_task():
    return run_async(dispatch_due_sources())
