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


async def test_dispatch_due_sources_enqueues_two_reporter_shards_when_concurrency_is_2(db, monkeypatch):
    from scraper.harvest_mode import set_harvest_mode
    from scraper.tasks.celery_app import app

    now = datetime.now(timezone.utc)
    sources = (await db.execute(select(ScraperSource))).scalars().all()
    for source in sources:
        source.next_scrape_at = now + timedelta(hours=1)
    target = next((s for s in sources if s.source_name == "PakistanLawSite"), None)
    assert target is not None
    target.next_scrape_at = now - timedelta(minutes=1)
    target.state = "ACTIVE"
    target.is_active = True
    await _activate_pls_slots(db, (1, 2))
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="shard enqueue")
    await db.commit()

    queued = []

    def fake_send_task(name, args=(), kwargs=None, queue=None):
        queued.append((name, args, kwargs or {}, queue))

    monkeypatch.setattr(app, "send_task", fake_send_task)
    result = await dispatch_due_sources()
    assert "PakistanLawSite:shard0" in result["queued"]
    assert "PakistanLawSite:shard1" in result["queued"]
    assert (
        "scraper.tasks.dispatcher.run_login_session_job",
        ("PakistanLawSite",),
        {"reporter_shard": 0},
        "login_session",
    ) in queued
    assert (
        "scraper.tasks.dispatcher.run_login_session_job",
        ("PakistanLawSite",),
        {"reporter_shard": 1},
        "login_session",
    ) in queued


async def test_run_source_allows_second_login_job_when_concurrency_is_2(db, monkeypatch):
    from scraper.harvest_mode import set_harvest_mode

    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "PakistanLawSite",
            )
        )
    ).scalars().first()
    assert source is not None
    source.state = "ACTIVE"
    source.is_active = True
    await _activate_pls_slots(db, (1, 2))  # a second job needs a second human login
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="dual lock holders")
    running = ScraperJob(
        source_id=source.id,
        source_name=source.source_name,
        job_type="scrape",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(running)
    await db.commit()

    async def fake_connector_for(_source):
        async def fake_connector(_source_obj, _db, **_kwargs):
            return {"pages": 1, "staged": 1}

        return fake_connector

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    result = await run_source(source.source_name, reporter_shard=1)
    assert result["staged"] == 1
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


async def test_dispatch_due_sources_runs_one_unsharded_job_when_only_one_slot_is_active(db, monkeypatch):
    """Two shards on one login would put two browsers on the same cookies and the site would end one
    of them; with a single ACTIVE slot the dispatcher must enqueue one unsharded job."""
    from scraper.harvest_mode import set_harvest_mode
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
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="single slot")
    await db.commit()

    queued = []

    def fake_send_task(name, args=(), kwargs=None, queue=None):
        queued.append((name, args, kwargs or {}, queue))

    monkeypatch.setattr(app, "send_task", fake_send_task)
    result = await dispatch_due_sources()
    assert result["queued"] == ["PakistanLawSite"]
    assert queued == [("scraper.tasks.dispatcher.run_login_session_job", ("PakistanLawSite",), {}, "login_session")]


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


async def test_login_session_max_active_is_capped_by_active_slots(db, monkeypatch):
    """Backfill targets two login-session workers, but every concurrent browser needs its own human
    login: with one ACTIVE slot only one job may run (two browsers on one login end each other)."""
    from scraper.harvest_mode import set_harvest_mode

    monkeypatch.setattr(settings, "BACKFILL_LOGIN_SESSION_CONCURRENCY", 2)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="cap by slots")
    await _activate_pls_slots(db, (1,))
    await db.commit()
    assert await dispatcher._login_session_max_active(db, "PakistanLawSite") == 1

    await _activate_pls_slots(db, (2,))
    await db.commit()
    assert await dispatcher._login_session_max_active(db, "PakistanLawSite") == 2


async def test_dispatch_due_sources_does_not_queue_second_job_on_single_slot(db, monkeypatch):
    """With one ACTIVE slot and a live running job, the dispatcher must not queue another
    PakistanLawSite job even though the backfill profile targets two."""
    from scraper.harvest_mode import set_harvest_mode
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
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    monkeypatch.setattr(settings, "BACKFILL_LOGIN_SESSION_CONCURRENCY", 2)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="single slot, job running")
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


async def test_dispatch_starts_only_the_missing_shard_when_one_shard_is_running(db, monkeypatch):
    """Slot 2 recovered while shard 0 still runs on slot 1: only shard 1 is started, never a second
    browser for shard 0."""
    from scraper.harvest_mode import set_harvest_mode
    from scraper.tasks.celery_app import app

    target = await _prepare_pls_due(db, (1, 2))
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    monkeypatch.setattr(settings, "BACKFILL_LOGIN_SESSION_CONCURRENCY", 2)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="one shard running")
    db.add(
        ScraperJob(
            source_id=target.id,
            source_name=target.source_name,
            job_type="scrape",
            status="running",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            result_summary={"reporter_shard": 0, "pages_charged": 12},
        )
    )
    await db.commit()
    queued = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None: queued.append((kwargs or {}).get("reporter_shard")))
    result = await dispatch_due_sources()
    assert result["queued"] == ["PakistanLawSite:shard1"] and queued == [1]


async def test_dispatch_starts_nothing_beside_a_running_unsharded_job(db, monkeypatch):
    """An unsharded job may fail over to any ACTIVE slot, so no shard is started while it runs,
    even after the second slot comes back."""
    from scraper.harvest_mode import set_harvest_mode
    from scraper.tasks.celery_app import app

    target = await _prepare_pls_due(db, (1, 2))
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    monkeypatch.setattr(settings, "BACKFILL_LOGIN_SESSION_CONCURRENCY", 2)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="unsharded running")
    db.add(ScraperJob(source_id=target.id, source_name=target.source_name, job_type="scrape", status="running", started_at=datetime.now(timezone.utc) - timedelta(minutes=5), result_summary={"reporter_shard": None}))
    await db.commit()
    queued = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None: queued.append(name))
    result = await dispatch_due_sources()
    assert result["queued"] == [] and queued == []
