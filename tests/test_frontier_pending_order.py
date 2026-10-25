"""crawl_frontier pending() claim order for public pipeline drains."""

from datetime import datetime, timedelta, timezone

import pytest

from scraper.models import CrawlFrontier
from scraper.tasks.public_pipeline import PublicPipeline


@pytest.mark.asyncio
async def test_pending_prefers_never_tried_over_high_attempt_deferred(db, source):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)
    deferred = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key="judgment:http://example.test/deferred-old",
        query_json={"kind": "judgment", "url": "http://example.test/deferred-old"},
        cursor_json={},
        status="pending",
        priority=50,
        attempts=7,
        next_run_at=now - timedelta(seconds=1),
        created_at=old,
    )
    fresh = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key="judgment:http://example.test/fresh",
        query_json={"kind": "judgment", "url": "http://example.test/fresh"},
        cursor_json={},
        status="pending",
        priority=50,
        attempts=0,
        next_run_at=None,
        created_at=now,
    )
    db.add(deferred)
    db.add(fresh)
    await db.commit()

    pipeline = PublicPipeline(db, source, fetcher=None)
    batch = await pipeline.pending(1)
    assert len(batch) == 1
    assert batch[0].query_key == fresh.query_key


@pytest.mark.asyncio
async def test_pending_skips_not_yet_due_high_attempt_rows(db, source):
    now = datetime.now(timezone.utc)
    not_due = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key="judgment:http://example.test/backoff",
        query_json={"kind": "judgment", "url": "http://example.test/backoff"},
        cursor_json={},
        status="pending",
        priority=50,
        attempts=7,
        next_run_at=now + timedelta(hours=1),
        created_at=now - timedelta(days=30),
    )
    due_fresh = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key="judgment:http://example.test/due",
        query_json={"kind": "judgment", "url": "http://example.test/due"},
        cursor_json={},
        status="pending",
        priority=50,
        attempts=0,
        next_run_at=None,
        created_at=now,
    )
    db.add(not_due)
    db.add(due_fresh)
    await db.commit()

    pipeline = PublicPipeline(db, source, fetcher=None)
    batch = await pipeline.pending(10)
    assert [r.query_key for r in batch] == [due_fresh.query_key]


@pytest.mark.asyncio
async def test_pending_tiebreaks_equal_attempts_by_priority_then_created_at(db, source):
    now = datetime.now(timezone.utc)
    older_low_prio = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key="judgment:http://example.test/older-low",
        query_json={"kind": "judgment", "url": "http://example.test/older-low"},
        cursor_json={},
        status="pending",
        priority=60,
        attempts=1,
        next_run_at=None,
        created_at=now - timedelta(hours=2),
    )
    newer_high_prio = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key="judgment:http://example.test/newer-high",
        query_json={"kind": "judgment", "url": "http://example.test/newer-high"},
        cursor_json={},
        status="pending",
        priority=40,
        attempts=1,
        next_run_at=None,
        created_at=now,
    )
    db.add(older_low_prio)
    db.add(newer_high_prio)
    await db.commit()

    pipeline = PublicPipeline(db, source, fetcher=None)
    batch = await pipeline.pending(2)
    assert [r.query_key for r in batch] == [newer_high_prio.query_key, older_low_prio.query_key]


@pytest.mark.asyncio
async def test_defer_priority_bump_respects_listing_base_priority(db, source, monkeypatch):
    from scraper.fetchers import FetchResult

    url = "http://127.0.0.1:9/listing"
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"listing:{url}",
        query_json={"kind": "listing", "url": url, "target_kind": "judgment", "depth": 0},
        cursor_json={},
        priority=30,
    )
    db.add(row)
    await db.flush()

    async def fake_fetch(_url):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=503,
            content=b"unavailable",
            content_type="text/html",
        )

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "pending"
    assert row.priority == 31
