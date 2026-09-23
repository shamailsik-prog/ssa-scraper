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
from scraper.harvest_mode import (
    backfill_progress,
    cadence_for_source,
    get_harvest_mode,
    login_pacing_profile,
    selected_source_names,
    set_harvest_mode,
    source_backfill_priority,
    source_selected_for_mode,
)
from scraper.models import BrowserSessionSlot, ScraperJob, ScraperSource
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


async def _active_running_jobs(
    db,
    source_name: str,
    *,
    now: datetime,
    max_active: int = 1,
) -> tuple[list[ScraperJob], bool]:
    """Return live running jobs while retiring stale/zombie running rows."""
    keep = max(1, int(max_active or 1))
    running_jobs = (
        await db.execute(
            select(ScraperJob).where(
                ScraperJob.source_name == source_name,
                ScraperJob.status == "running",
            )
        )
    ).scalars().all()
    if not running_jobs:
        return [], False
    stale_cutoff = now - RUNNING_JOB_STALE_AFTER
    heartbeat_cutoff = now - RUNNING_JOB_HEARTBEAT_STALE_AFTER
    running_jobs.sort(
        key=lambda job: _running_started_at(job) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    active_jobs: list[ScraperJob] = []
    mutated = False
    for job in running_jobs:
        started_at = _running_started_at(job)
        heartbeat_at = _running_heartbeat_at(job)
        stale = started_at is None or started_at <= stale_cutoff
        if not stale and heartbeat_at is not None and heartbeat_at <= heartbeat_cutoff:
            stale = True
        if len(active_jobs) >= keep:
            stale = True
        if not stale:
            active_jobs.append(job)
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
            "Recovered stale running job source=%s job_id=%s started_at=%s active_jobs=%s",
            source_name,
            job.id,
            started_at,
            [active.id for active in active_jobs],
        )
    return active_jobs, mutated


async def _active_running_job(db, source_name: str, *, now: datetime) -> tuple[Optional[ScraperJob], bool]:
    """Return the newest live running job while retiring stale/zombie running rows."""
    active_jobs, mutated = await _active_running_jobs(db, source_name, now=now, max_active=1)
    return (active_jobs[0] if active_jobs else None), mutated


async def _login_session_max_active(db, source_name: Optional[str] = None) -> int:
    """How many login-session jobs may run at once for a source: the pacing profile's target,
    capped by the number of ACTIVE slots. Each concurrent browser needs its own human login; two
    browsers on one login share one server-side session and the site ends one of them."""
    mode = await get_harvest_mode(db)
    target = int(login_pacing_profile(mode)["login_session_concurrency"] or 1)
    if source_name is None or target <= 1:
        return max(1, target)
    active_slots = len(await _active_slot_numbers(db, source_name))
    return max(1, min(target, active_slots))


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
        max_active = 1
        if source.access_method == "login_session":
            max_active = await _login_session_max_active(db, source_name)
        existing_jobs, recovered_stale = await _active_running_jobs(
            db,
            source_name,
            now=datetime.now(timezone.utc),
            max_active=max_active,
        )
        if len(existing_jobs) >= max_active:
            if recovered_stale:
                await db.commit()
            logger.info(
                "%s already has %s running job(s); skipping duplicate kick",
                source_name,
                len(existing_jobs),
            )
            return {"skipped": "already_running", "job_id": str(existing_jobs[0].id)}
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
def run_login_session_job(self, source_name: str = "PakistanLawSite", reporter_shard=None):
    """Login-session queue. When concurrency is 2, Beat enqueues one job per reporter shard."""
    return run_async(run_source(source_name, reporter_shard=reporter_shard))


async def _active_slot_numbers(db, source_name: str) -> list[int]:
    rows = (
        await db.execute(
            select(BrowserSessionSlot.slot_number).where(
                BrowserSessionSlot.source_name == source_name,
                BrowserSessionSlot.state == "ACTIVE",
            )
        )
    ).scalars().all()
    return sorted(int(n) for n in rows)


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
            concurrency = 1
            if s.access_method == "login_session":
                concurrency = await _login_session_max_active(db, s.source_name)
            running_jobs, _ = await _active_running_jobs(db, s.source_name, now=now, max_active=concurrency)
            if len(running_jobs) >= concurrency:
                logger.info(
                    "skip enqueue %s: %s login-session scrape job(s) already running",
                    s.source_name,
                    len(running_jobs),
                )
                continue
            if s.access_method == "login_session" and concurrency >= 2 and len(await _active_slot_numbers(db, s.source_name)) >= 2:
                # Two shards only when both slots hold a human login: each shard runs on its own slot.
                app.send_task(
                    "scraper.tasks.dispatcher.run_login_session_job",
                    args=(s.source_name,),
                    kwargs={"reporter_shard": 0},
                    queue="login_session",
                )
                app.send_task(
                    "scraper.tasks.dispatcher.run_login_session_job",
                    args=(s.source_name,),
                    kwargs={"reporter_shard": 1},
                    queue="login_session",
                )
                queued.append(f"{s.source_name}:shard0")
                queued.append(f"{s.source_name}:shard1")
            elif s.access_method == "login_session":
                if concurrency >= 2:
                    logger.info("%s: only one ACTIVE slot; running a single unsharded login-session job", s.source_name)
                app.send_task("scraper.tasks.dispatcher.run_login_session_job", args=(s.source_name,), queue="login_session")
                queued.append(s.source_name)
            else:
                app.send_task("scraper.tasks.dispatcher.run_source_job", args=(s.source_name,), queue="scraper")
                queued.append(s.source_name)
            s.next_scrape_at = now + timedelta(minutes=cadence_for_source(s, mode))
        await db.commit()
    return {"mode": mode, "auto_switched": auto_switched, "queued": queued}


@shared_task(name="scraper.tasks.dispatcher.dispatch_due_sources")
def dispatch_due_sources_task():
    return run_async(dispatch_due_sources())
