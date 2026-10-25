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
