from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from scraper.config import settings
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


async def _activate_pls_slots(db, slots):
    from scraper.models import BrowserSessionSlot

    rows = (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite"))).scalars().all()
    for row in rows:
        if row.slot_number in slots:
            row.state = "ACTIVE"
            row.storage_state_encrypted = settings.encrypt_value("{}")
    await db.flush()


async def test_run_source_retires_running_job_without_heartbeat(db, monkeypatch):
    """A job whose worker container was recreated keeps status=running with a stale updated_at; it
    must not block its source for the full three-hour age cut-off."""
    from sqlalchemy import update

    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
    started = datetime.now(timezone.utc) - timedelta(minutes=45)
    dead = ScraperJob(source_id=source.id, source_name=source.source_name, job_type="scrape", status="running", started_at=started)
    db.add(dead)
    await db.commit()
    silent_since = datetime.now(timezone.utc) - dispatcher.RUNNING_JOB_HEARTBEAT_STALE_AFTER - timedelta(minutes=1)
    await db.execute(update(ScraperJob).where(ScraperJob.id == dead.id).values(updated_at=silent_since, created_at=started))
    await db.commit()

    async def fake_connector_for(_source):
        async def fake_connector(_source_obj, _db, **_kwargs):
            return {"pages": 1, "staged": 1}

        return fake_connector

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    result = await run_source(source.source_name)
    assert result["staged"] == 1
    await db.refresh(dead)
    assert dead.status == "failed"
    assert "no heartbeat" in (dead.error_message or "")


async def test_run_source_keeps_running_job_with_recent_heartbeat(db):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    live = ScraperJob(source_id=source.id, source_name=source.source_name, job_type="scrape", status="running", started_at=started)
    db.add(live)
    await db.commit()
    # updated_at defaults to now: the job is old but still heartbeating.
    result = await run_source(source.source_name)
    assert result == {"skipped": "already_running", "job_id": str(live.id)}


async def test_dispatch_due_sources_does_not_queue_second_job_on_single_slot(db, monkeypatch):
    """One login-session job at a time: with a live running job the dispatcher must not queue
    another PakistanLawSite job."""
    from scraper.tasks.celery_app import app

    now = datetime.now(timezone.utc)
    sources = (await db.execute(select(ScraperSource))).scalars().all()
    for source in sources:
        source.next_scrape_at = now + timedelta(hours=1)
    target = next((s for s in sources if s.source_name == "PakistanLawSite"), None)
    target.next_scrape_at = now - timedelta(minutes=1)
    target.state = "ACTIVE"
    target.is_active = True
    await _activate_pls_slots(db, (1,))
    db.add(
        ScraperJob(
            source_id=target.id,
            source_name=target.source_name,
            job_type="scrape",
            status="running",
            started_at=now - timedelta(minutes=5),
        )
    )
    await db.commit()

    queued = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None: queued.append(name))
    result = await dispatch_due_sources()
    assert result["queued"] == []
    assert queued == []


async def _prepare_pls_due(db, slots):
    now = datetime.now(timezone.utc)
    sources = (await db.execute(select(ScraperSource))).scalars().all()
    for source in sources:
        source.next_scrape_at = now + timedelta(hours=1)
    target = next((s for s in sources if s.source_name == "PakistanLawSite"), None)
    target.next_scrape_at = now - timedelta(minutes=1)
    target.state = "ACTIVE"
    target.is_active = True
    await _activate_pls_slots(db, slots)
    return target


async def test_worker_start_retires_orphaned_login_session_jobs(db):
    """A deploy recreates the login-session worker while a job runs; the row must not block the
    source for the heartbeat cut-off. Public-source jobs (other workers) are left alone."""
    now = datetime.now(timezone.utc)
    pls = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    pub = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
    dead = ScraperJob(source_id=pls.id, source_name=pls.source_name, job_type="scrape", status="running", started_at=now - timedelta(minutes=3))
    alive_public = ScraperJob(source_id=pub.id, source_name=pub.source_name, job_type="scrape", status="running", started_at=now - timedelta(minutes=3))
    db.add_all([dead, alive_public])
    await db.commit()
    import redis.asyncio as aioredis

    r = aioredis.from_url(settings.REDIS_URL)
    base = "corpus:login_session_lock:PakistanLawSite"
    await r.set(base, "dead-token", ex=3600)
    await r.set("corpus:login_session_lock:Other", "keep", ex=60)
    try:
        assert await dispatcher.retire_orphaned_login_jobs(redis_client=r) == 1
        assert await r.exists(base) == 0  # the dead job's lock is gone
        assert await r.exists("corpus:login_session_lock:Other") == 1  # unrelated keys untouched
    finally:
        await r.delete("corpus:login_session_lock:Other")
        await r.aclose()
    async with __import__("scraper.database", fromlist=["SessionLocal"]).SessionLocal() as fresh:
        rows = {j.source_name: j for j in (await fresh.execute(select(ScraperJob))).scalars().all()}
        assert rows["PakistanLawSite"].status == "failed" and "worker started" in rows["PakistanLawSite"].error_message
        assert rows["PakistanCode"].status == "running"
