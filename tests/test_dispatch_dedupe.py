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


async def test_queued_duplicates_are_skipped_once_their_source_has_run(db, monkeypatch):
    """The 440 leftover tasks: one that waited while its source ran again is skipped without a job."""
    from scraper.tasks.dispatcher import run_queued_source, superseded_by_later_job

    monkeypatch.setattr(settings, "DISPATCH_DEDUPE_ENABLED", True)
    ran = []

    async def fake_run_source(name, **kw):
        ran.append(name)
        return {"ran": name}

    monkeypatch.setattr(dispatcher, "run_source", fake_run_source)
    src = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "GBAssembly"))).scalars().first()
    now = datetime.now(timezone.utc)
    queued_at = now - timedelta(minutes=30)
    db.add(ScraperJob(source_id=src.id, source_name="GBAssembly", job_type="scrape", status="done", started_at=now - timedelta(minutes=5)))
    await db.commit()

    assert await superseded_by_later_job(db, "GBAssembly", queued_at) is True
    assert await superseded_by_later_job(db, "GBAssembly", now) is False
    assert (await run_queued_source("GBAssembly", queued_at=queued_at.isoformat()))["skipped"] == "superseded"
    assert (await run_queued_source("GBAssembly", queued_at=None))["skipped"] == "superseded"  # legacy task, ran 5 min ago
    assert ran == []
    assert (await run_queued_source("GBAssembly", queued_at=now.isoformat()))["ran"] == "GBAssembly"
    assert (await run_queued_source("GBAssembly", manual=True))["ran"] == "GBAssembly"  # "run now" always runs


async def test_a_source_that_has_not_run_for_hours_runs_its_legacy_task(db, monkeypatch):
    from scraper.tasks.dispatcher import superseded_by_later_job

    src = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
    db.add(ScraperJob(source_id=src.id, source_name="PakistanCode", job_type="scrape", status="done", started_at=datetime.now(timezone.utc) - timedelta(hours=6)))
    await db.commit()
    # judged an hour from now, so jobs other tests started a moment ago cannot count as recent
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert await superseded_by_later_job(db, "PakistanCode", None, now=later) is False


async def test_nul_in_any_text_or_json_column_never_reaches_postgres(db):
    """8 October 2026: a link read out of a .docx put \\x00 into crawl_frontier.query_json and the INSERT
    ('unsupported Unicode escape sequence') killed the job. Every flush now strips NUL."""
    from scraper.models import CrawlFrontier

    fr = CrawlFrontier(
        source_name="SindhAssembly",
        tier=0,
        query_key="listing:nul-test\x00",
        query_json={"kind": "listing", "url": "https://example.test/a\x00b", "list": ["x\x00"]},
        cursor_json={},
        priority=40,
        last_error="bad\x00byte",
    )
    db.add(fr)
    await db.flush()
    await db.refresh(fr)
    assert fr.query_key == "listing:nul-test"
    assert fr.query_json == {"kind": "listing", "url": "https://example.test/ab", "list": ["x"]}
    assert fr.last_error == "badbyte"
    await db.rollback()


def test_legacy_queued_tasks_are_skipped_when_their_source_ran_in_the_last_six_hours():
    assert settings.DISPATCH_LEGACY_SKIP_SECONDS >= 6 * 3600


async def test_nul_inside_a_tuple_is_stripped_and_unchanged_stored_values_are_not_rescanned(db):
    from scraper.models import CrawlFrontier

    fr = CrawlFrontier(source_name="SindhAssembly", tier=0, query_key="listing:tuple-test", query_json={"items": ("a\x00b",)}, cursor_json={}, priority=40)
    db.add(fr)
    await db.flush()
    await db.refresh(fr)
    assert fr.query_json == {"items": ["ab"]}
    # a stored row whose only change is its status: the listener must not even look at its other columns
    from scraper import database

    seen = []
    real = database._has_nul
    database._has_nul = lambda value: (seen.append(value), real(value))[1]
    try:
        fr.status = "done"
        await db.flush()
    finally:
        database._has_nul = real
    assert seen == ["done"]
    await db.rollback()


async def test_frontier_lookup_with_a_nul_key_finds_the_row_stored_without_it(db):
    """Cursor Agent's PgSafeString: an existence check made with the raw key (NUL included) must match the
    row whose key was cleaned at flush, or the same link is queued again on every listing pass."""
    from scraper.models import CrawlFrontier

    raw = "listing:https://example.test/a\x00b"
    db.add(CrawlFrontier(source_name="SindhAssembly", tier=0, query_key=raw, query_json={}, cursor_json={}, priority=40))
    await db.flush()
    found = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.source_name == "SindhAssembly", CrawlFrontier.query_key == raw))).scalars().first()
    assert found is not None and found.query_key == "listing:https://example.test/ab"
    await db.rollback()


async def test_a_superseded_duplicate_leaves_the_newer_tasks_queued_mark(db, monkeypatch):
    from scraper.tasks.dispatcher import run_source

    monkeypatch.setattr(settings, "DISPATCH_DEDUPE_ENABLED", True)
    src = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "GBAssembly"))).scalars().first()
    now = datetime.now(timezone.utc)
    db.add(ScraperJob(source_id=src.id, source_name="GBAssembly", job_type="scrape", status="done", started_at=now - timedelta(minutes=1)))
    await db.commit()
    _redis().set(dispatcher._queued_key("GBAssembly"), "newer task", ex=60)
    result = await run_source("GBAssembly", check_superseded=True, queued_at=now - timedelta(minutes=10))
    assert result["skipped"] == "superseded"
    assert _redis().get(dispatcher._queued_key("GBAssembly")) == b"newer task"
    _redis().delete(dispatcher._queued_key("GBAssembly"))


async def test_an_over_long_frontier_key_is_stored_shortened_and_found_again(db):
    """8 October 2026: IslamabadHighCourt listing keys embed whole judgment URLs; one over 500 characters failed
    the INSERT (StringDataRightTruncationError) and rolled back every run of the source. Over-long keys are
    shortened deterministically (prefix + hash), so lookups with the raw key still find the row and two keys that
    share a long prefix stay distinct."""
    from scraper.models import CrawlFrontier

    base = "listing:https://mis.ihc.gov.pk/frmRdJgmnt.aspx?cseNo=" + "x" * 600
    a, b = base + "-A.pdf", base + "-B.pdf"
    for key in (a, b):
        db.add(CrawlFrontier(source_name="IslamabadHighCourt", tier=0, query_key=key, query_json={}, cursor_json={}, priority=40))
    await db.flush()
    for key in (a, b):
        rows = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.source_name == "IslamabadHighCourt", CrawlFrontier.query_key == key))).scalars().all()
        assert len(rows) == 1 and len(rows[0].query_key) <= 500 and rows[0].query_key.startswith("listing:https://mis.ihc.gov.pk/")
    await db.rollback()


async def test_seeding_twice_with_an_over_long_statute_name_stays_idempotent(db):
    """#183 review (Codex): seed_frontier and seed_extended_plan compare stored keys with freshly built ones in
    Python. A statute name may be 1,000 characters, so a key over 500 is stored shortened; the next seed must
    build the same shortened key, or it inserts it again and the unique constraint aborts the flush."""
    from scraper.models import Statute, StatuteSection
    from scraper.tasks.pakistanlawsite import seed_frontier
    from scraper.tasks.pls_search_harvest import seed_extended_plan

    st = Statute(name="The " + "Very Long Title " * 40 + "Act", source_name="PakistanCode")
    db.add(st)
    await db.flush()
    db.add(StatuteSection(statute_id=st.id, section_number="1"))
    await db.flush()
    for _ in range(2):
        await seed_frontier(db, None)
        await seed_extended_plan(db, judge_limit=0)
        await db.flush()
    await db.rollback()
