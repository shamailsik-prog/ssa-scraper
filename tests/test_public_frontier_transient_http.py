"""Transient HTTP responses must not permanently remove judgment frontier rows.

The real PublicPipeline state transition is exercised with a stubbed fetch.
No licensed or public court site is contacted.
"""

import pytest

from scraper.fetchers import FetchResult
from scraper.models import CrawlFrontier
from scraper.tasks.public_pipeline import PublicPipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [406, 408, 429, 500, 503, 504])
@pytest.mark.parametrize("prior_attempts", [9, 15])
async def test_transient_http_keeps_judgment_pending_after_attempt_cap(
    db, source, monkeypatch, status_code, prior_attempts
):
    url = f"http://127.0.0.1:9/judgment/transient-{status_code}-{prior_attempts}.pdf"
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={"kind": "judgment", "url": url},
        cursor_json={},
        status="pending",
        attempts=prior_attempts,
        priority=50,
    )
    db.add(row)
    await db.flush()

    async def fake_fetch(_url):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=status_code,
            content=b"temporarily unavailable",
            content_type="text/plain",
        )

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "pending"
    assert row.attempts == prior_attempts + 1
    assert row.last_error == f"HTTP {status_code}"
    assert row.next_run_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 410])
async def test_confirmed_not_found_retirement_is_unchanged(db, source, monkeypatch, status_code):
    url = f"http://127.0.0.1:9/judgment/gone-{status_code}.pdf"
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={"kind": "judgment", "url": url},
        cursor_json={},
        status="pending",
        attempts=9,
        priority=50,
    )
    db.add(row)
    await db.flush()

    async def fake_fetch(_url):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=status_code,
            content=b"not found",
            content_type="text/plain",
        )

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "retired"
    assert row.last_error == f"HTTP {status_code}"
