"""Updates-mode dispatch backlog override (tier-0 frontier) with pipeline-aligned progress guards."""

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


async def _seed_finished_job(
    db,
    source: ScraperSource,
    *,
    completed: int = 0,
    backlog_remaining: bool = False,
    finished_at: datetime | None = None,
):
    finished_at = finished_at or datetime.now(timezone.utc)
    db.add(
        ScraperJob(
            source_id=source.id,
            source_name=source.source_name,
            job_type="scrape",
            status="done",
            started_at=finished_at - timedelta(minutes=5),
            finished_at=finished_at,
            result_summary={"completed": completed, "backlog_remaining": backlog_remaining},
        )
    )


async def _pending_tier0(db, source_name: str, key: str):
    db.add(
        CrawlFrontier(
            source_name=source_name,
            tier=0,
            query_key=key,
            query_json={"kind": "judgment", "url": f"https://example.test/{key}"},
            cursor_json={},
            priority=10,
            status="pending",
        )
    )


async def test_updates_mode_queues_public_source_with_pending_frontier_even_when_not_scheduled_due(
    db, monkeypatch
):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="backlog due test")
    shc = await _only_shc_due(db)
    await _seed_finished_job(db, shc, completed=2, backlog_remaining=True)
    await _pending_tier0(db, "SindhHighCourt", "judgment:test-backlog-due")
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))

    result = await dispatch_due_sources()
    assert "SindhHighCourt" in result["queued"]
    assert ("SindhHighCourt",) in sent
    await db.refresh(shc)
    wait = int(settings.PUBLIC_BACKLOG_CONTINUE_SECONDS or 60)
    assert shc.next_scrape_at <= datetime.now(timezone.utc) + timedelta(seconds=wait + 5)


async def test_lost_task_after_recreate_queues_when_last_job_older_than_cadence(db, monkeypatch):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="lost task")
    shc = await _only_shc_due(db)
    cadence_hours = int((shc.config_json or {}).get("update_frequency_hours") or settings.UPDATE_CADENCE_HOURS)
    old = datetime.now(timezone.utc) - timedelta(hours=cadence_hours + 1)
    await _seed_finished_job(db, shc, completed=0, backlog_remaining=False, finished_at=old)
    await _pending_tier0(db, "SindhHighCourt", "judgment:test-lost-task")
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" in result["queued"]


async def test_completed_job_with_backlog_stays_due_for_dispatch(db, monkeypatch):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="backlog remain due")
    shc = await _only_shc_due(db)
    await _pending_tier0(db, "SindhHighCourt", "judgment:test-backlog-remain")
    await db.commit()

    async def fake_connector_for(_source):
        async def fake_connector(_source_obj, _db, **_kwargs):
            await _continue_while_backlog(
                _db,
                _source_obj,
                {"limit_reached": True, "completed": 2},
            )
            return {
                "fetched": 40,
                "staged": 0,
                "deferred": 38,
                "completed": 2,
                "limit_reached": True,
                "backlog_remaining": True,
            }

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
    await _seed_finished_job(db, shc, completed=1, backlog_remaining=True, finished_at=now - timedelta(hours=1))
    await _pending_tier0(db, "SindhHighCourt", "judgment:test-stale-running")
    stale_started = now - dispatcher.RUNNING_JOB_STALE_AFTER - timedelta(minutes=1)
    stale = ScraperJob(
        source_id=shc.id,
        source_name="SindhHighCourt",
        job_type="scrape",
        status="running",
        started_at=stale_started,
    )
    db.add(stale)
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" in result["queued"]
    assert ("SindhHighCourt",) in sent
    await db.refresh(stale)
    assert stale.status == "failed"
    assert stale.finished_at is not None


async def test_no_frontier_does_not_force_due(db, monkeypatch):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="no frontier")
    shc = await _only_shc_due(db)
    await _seed_finished_job(db, shc, completed=5, backlog_remaining=True)
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" not in result["queued"]
    assert sent == []


async def test_tier_above_zero_pending_does_not_force_due(db, monkeypatch):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="tier gt0")
    shc = await _only_shc_due(db)
    await _seed_finished_job(db, shc, completed=3, backlog_remaining=True)
    db.add(
        CrawlFrontier(
            source_name="SindhHighCourt",
            tier=1,
            query_key="judgment:tier1-only",
            query_json={"kind": "judgment", "url": "https://example.test/tier1"},
            cursor_json={},
            priority=10,
            status="pending",
        )
    )
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" not in result["queued"]


async def test_done_frontier_does_not_force_due(db, monkeypatch):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="done frontier")
    shc = await _only_shc_due(db)
    await _seed_finished_job(db, shc, completed=2, backlog_remaining=True)
    db.add(
        CrawlFrontier(
            source_name="SindhHighCourt",
            tier=0,
            query_key="judgment:done-only",
            query_json={"kind": "judgment", "url": "https://example.test/done"},
            cursor_json={},
            priority=10,
            status="done",
        )
    )
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" not in result["queued"]


async def test_login_source_not_subject_to_public_backlog_override(db, monkeypatch):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="login exempt")
    now = datetime.now(timezone.utc)
    for s in (await db.execute(select(ScraperSource))).scalars().all():
        s.next_scrape_at = now + timedelta(days=7)
        s.state, s.is_active = "ACTIVE", True
    pls = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    pls.next_scrape_at = now + timedelta(days=7)
    await _seed_finished_job(db, pls, completed=10, backlog_remaining=True)
    await _pending_tier0(db, "PakistanLawSite", "judgment:pls-frontier")
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "PakistanLawSite" not in result["queued"]


async def test_backfill_mode_does_not_apply_backlog_override(db, monkeypatch):
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="backfill no override")
    shc = await _only_shc_due(db)
    await _seed_finished_job(db, shc, completed=2, backlog_remaining=True)
    await _pending_tier0(db, "SindhHighCourt", "judgment:backfill-mode")
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" not in result["queued"]


async def test_recent_zero_progress_job_does_not_force_due(db, monkeypatch):
    await set_harvest_mode(db, "updates", changed_by="qa", reason="zero progress")
    shc = await _only_shc_due(db)
    await _seed_finished_job(db, shc, completed=0, backlog_remaining=False, finished_at=datetime.now(timezone.utc))
    await _pending_tier0(db, "SindhHighCourt", "judgment:zero-progress")
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" not in result["queued"]


async def test_backlog_continue_seconds_zero_disables_override(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BACKLOG_CONTINUE_SECONDS", 0)
    await set_harvest_mode(db, "updates", changed_by="qa", reason="setting zero")
    shc = await _only_shc_due(db)
    await _seed_finished_job(db, shc, completed=5, backlog_remaining=True)
    await _pending_tier0(db, "SindhHighCourt", "judgment:setting-zero")
    await db.commit()

    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))
    result = await dispatch_due_sources()
    assert "SindhHighCourt" not in result["queued"]
