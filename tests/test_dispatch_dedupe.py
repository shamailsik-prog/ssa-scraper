"""Dispatch never queues a public source twice while its earlier job has not started; a job whose flush
fails is recorded as failed instead of staying 'running'; NUL bytes never reach PostgreSQL.

8 October 2026: during a two-hour OCR stall the dispatcher queued every public source again every
cadence; afterwards the workers spent hours on near-empty duplicate runs and PakistanCode's job sat at the
back of the queue. The same day a .docx read as text (NUL bytes) failed its INSERT, the error path raised
PendingRollbackError and the BalochistanAssembly job stayed 'running'."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from scraper.config import settings
from scraper.models import ScraperJob, ScraperSource
from scraper.tasks import dispatcher
from scraper.tasks.chain import _redis
from scraper.tasks.dispatcher import dispatch_due_sources, run_source


async def _only_due(db, name):
    now = datetime.now(timezone.utc)
    for s in (await db.execute(select(ScraperSource))).scalars().all():
        s.next_scrape_at = now + timedelta(hours=1)
        if s.source_name == name:
            s.next_scrape_at = now - timedelta(minutes=1)
            s.state, s.is_active = "ACTIVE", True
    await db.commit()


async def test_a_source_already_queued_is_not_queued_again(db, monkeypatch):
    from scraper.tasks.celery_app import app

    monkeypatch.setattr(settings, "DISPATCH_DEDUPE_ENABLED", True)
    _redis().delete(dispatcher._queued_key("PakistanCode"))
    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append(args))

    await _only_due(db, "PakistanCode")
    first = await dispatch_due_sources()
    assert "PakistanCode" in first["queued"] and sent == [("PakistanCode",)]

    await _only_due(db, "PakistanCode")  # due again before the queued job started
    second = await dispatch_due_sources()
    assert "PakistanCode" not in second["queued"] and sent == [("PakistanCode",)]
    assert "PakistanCode:already_queued" in second["skipped_saturated"]

    dispatcher.clear_source_queued("PakistanCode")  # what run_source does when the job starts
    await _only_due(db, "PakistanCode")
    third = await dispatch_due_sources()
    assert "PakistanCode" in third["queued"] and len(sent) == 2
    _redis().delete(dispatcher._queued_key("PakistanCode"))


async def test_a_job_whose_flush_fails_is_recorded_as_failed(db, monkeypatch):
    async def broken_connector(source, session, job_id=None, **kw):
        # what the server hit: text with a NUL byte fails the INSERT during flush
        session.add(ScraperJob(source_id=source.id, source_name=source.source_name, job_type="scrape", status="done", error_message="PK\x03\x04\x00"))
        await session.flush()

    async def connector_for(source):
        return broken_connector

    monkeypatch.setattr(dispatcher, "_connector_for", connector_for)
    await _only_due(db, "PunjabAssembly")
    stats = await run_source("PunjabAssembly")
    assert "error" in stats
    db.expire_all()
    jobs = (await db.execute(select(ScraperJob).where(ScraperJob.source_name == "PunjabAssembly"))).scalars().all()
    assert jobs and all(j.status == "failed" for j in jobs)


def test_nul_bytes_are_removed_before_staging():
    from scraper.fetchers import _pg_text

    assert _pg_text("PK\x03\x04\x00\x00ok") == "PK\x03\x04ok"
    assert _pg_text(None) is None
