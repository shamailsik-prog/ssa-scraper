from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from scraper.models import ScraperJob, ScraperSource
from scraper.tasks import dispatcher
from scraper.tasks.dispatcher import dispatch_due_sources, run_source


async def test_run_source_skips_when_existing_job_is_running(db):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "PakistanCode",
            )
        )
    ).scalars().first()
    assert source is not None

    running = ScraperJob(
        source_id=source.id,
        source_name=source.source_name,
        job_type="scrape",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(running)
    await db.commit()

    result = await run_source(source.source_name)
    assert result == {"skipped": "already_running", "job_id": str(running.id)}

    running_jobs = (
        await db.execute(
            select(func.count())
            .select_from(ScraperJob)
            .where(
                ScraperJob.source_name == source.source_name,
                ScraperJob.status == "running",
            )
        )
    ).scalar()
    assert running_jobs == 1


async def test_run_source_replaces_stale_running_job(db, monkeypatch):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "PakistanCode",
            )
        )
    ).scalars().first()
    assert source is not None

    stale_started = datetime.now(timezone.utc) - dispatcher.RUNNING_JOB_STALE_AFTER - timedelta(minutes=1)
    stale_running = ScraperJob(
        source_id=source.id,
        source_name=source.source_name,
        job_type="scrape",
        status="running",
        started_at=stale_started,
    )
    db.add(stale_running)
    await db.commit()

    async def fake_connector_for(_source):
        async def fake_connector(_source_obj, _db, **_kwargs):
            return {"pages": 1, "staged": 1}

        return fake_connector

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    result = await run_source(source.source_name)

    assert result["staged"] == 1
    await db.refresh(stale_running)
    assert stale_running.status == "failed"
    assert stale_running.finished_at is not None
    jobs = (
        await db.execute(
            select(ScraperJob).where(
                ScraperJob.source_name == source.source_name,
            )
        )
    ).scalars().all()
    done_jobs = [job for job in jobs if job.status == "done"]
    assert len(done_jobs) == 1


async def test_dispatch_due_sources_queues_source_after_retiring_stale_running_job(db, monkeypatch):
    now = datetime.now(timezone.utc)
    sources = (await db.execute(select(ScraperSource))).scalars().all()
    for source in sources:
        source.next_scrape_at = now + timedelta(hours=1)
    target = next((s for s in sources if s.source_name == "PakistanCode"), None)
    assert target is not None
    target.next_scrape_at = now - timedelta(minutes=1)
    target.state = "ACTIVE"
    target.is_active = True
    stale_started = now - dispatcher.RUNNING_JOB_STALE_AFTER - timedelta(minutes=1)
    stale_running = ScraperJob(
        source_id=target.id,
        source_name=target.source_name,
        job_type="scrape",
        status="running",
        started_at=stale_started,
    )
    db.add(stale_running)
    await db.commit()

    from scraper.tasks.celery_app import app

    queued = []

    def fake_send_task(name, args=(), queue=None):
        queued.append((name, args, queue))

    monkeypatch.setattr(app, "send_task", fake_send_task)

    result = await dispatch_due_sources()
    assert "PakistanCode" in result["queued"]
    assert ("scraper.tasks.dispatcher.run_source_job", ("PakistanCode",), "scraper") in queued
    await db.refresh(stale_running)
    assert stale_running.status == "failed"
