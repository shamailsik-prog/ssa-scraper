"""Updates mode must keep draining public sources while crawl_frontier still has tier-0 work.

After a deploy recreates workers, a lost queue task and a next_scrape_at pushed out by the full
update cadence can leave sources like SindhHighCourt idle for hours despite pending frontier rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from scraper.config import settings
from scraper.harvest_mode import set_harvest_mode
from scraper.models import CrawlFrontier, ScraperJob, ScraperSource
from scraper.tasks.celery_app import app
from scraper.tasks import dispatcher
from scraper.tasks.dispatcher import dispatch_due_sources, run_source
from scraper.tasks.public_pipeline import _continue_while_backlog


async def _only_shc_due(db):
    now = datetime.now(timezone.utc)
    for s in (await db.execute(select(ScraperSource))).scalars().all():
        s.next_scrape_at = now + timedelta(days=7)
        s.state, s.is_active = "ACTIVE", True
    shc = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "SindhHighCourt"))).scalars().first()
    assert shc is not None
    shc.next_scrape_at = now + timedelta(days=7)
    await db.commit()
    return shc


async def test_updates_mode_queues_public_source_with_pending_frontier_even_when_not_scheduled_due(
    db, monkeypatch
):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="backlog due test")
    shc = await _only_shc_due(db)
    db.add(
        CrawlFrontier(
            source_name="SindhHighCourt",
            tier=0,
            query_key="judgment:test-backlog-due",
            query_json={"kind": "judgment", "url": "https://caselaw.shc.gov.pk/example"},
            cursor_json={},
            priority=10,
            status="pending",
        )
    )
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))

    result = await dispatch_due_sources()
    assert "SindhHighCourt" in result["queued"]
    assert ("SindhHighCourt",) in sent
    await db.refresh(shc)
    wait = int(settings.PUBLIC_BACKLOG_CONTINUE_SECONDS or 60)
    assert shc.next_scrape_at <= datetime.now(timezone.utc) + timedelta(seconds=wait + 5)


async def test_completed_job_with_backlog_stays_due_for_dispatch(db, monkeypatch):
    """A run that sets backlog_remaining must not be starved by a far-future next_scrape_at."""
    await set_harvest_mode(db, "updates", changed_by="qa", reason="backlog remain due")
    shc = await _only_shc_due(db)
    db.add(
        CrawlFrontier(
            source_name="SindhHighCourt",
            tier=0,
            query_key="judgment:test-backlog-remain",
            query_json={"kind": "judgment", "url": "https://caselaw.shc.gov.pk/example2"},
            cursor_json={},
            priority=10,
            status="pending",
        )
    )
    await db.commit()

    async def fake_connector_for(_source):
        async def fake_connector(_source_obj, _db, **_kwargs):
            await _continue_while_backlog(
                _db,
                _source_obj,
                {"limit_reached": True, "completed": 2},
            )
            return {"fetched": 40, "staged": 0, "deferred": 38, "completed": 2, "limit_reached": True, "backlog_remaining": True}

        return fake_connector

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    stats = await run_source("SindhHighCourt")
    assert stats.get("backlog_remaining") is True
    await db.refresh(shc)
    wait = int(settings.PUBLIC_BACKLOG_CONTINUE_SECONDS or 60)
    assert shc.next_scrape_at <= datetime.now(timezone.utc) + timedelta(seconds=wait + 5)

    shc.next_scrape_at = datetime.now(timezone.utc) + timedelta(days=7)
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" in result["queued"]
    assert ("SindhHighCourt",) in sent


async def test_stale_running_job_with_frontier_backlog_is_requeued(db, monkeypatch):
    now = datetime.now(timezone.utc)
    await set_harvest_mode(db, "updates", changed_by="qa", reason="stale running backlog")
    shc = await _only_shc_due(db)
    db.add(
        CrawlFrontier(
            source_name="SindhHighCourt",
            tier=0,
            query_key="judgment:test-stale-running",
            query_json={"kind": "judgment", "url": "https://caselaw.shc.gov.pk/example3"},
            cursor_json={},
            priority=10,
            status="pending",
        )
    )
    stale_started = now - dispatcher.RUNNING_JOB_STALE_AFTER - timedelta(minutes=1)
    db.add(
        ScraperJob(
            source_id=shc.id,
            source_name="SindhHighCourt",
            job_type="scrape",
            status="running",
            started_at=stale_started,
        )
    )
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" in result["queued"]
    assert ("SindhHighCourt",) in sent
