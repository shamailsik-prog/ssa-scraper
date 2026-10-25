"""PDF-shaped judgment URLs that return HTML or temporary non-PDF bodies."""

from unittest.mock import AsyncMock

import pytest

from scraper.fetchers import FetchResult, HttpFetcher
from scraper.judgment_document_body import PDF_MISS_DEFER_ERROR, html_judgment_word_count
from scraper.models import CrawlFrontier
from scraper.tasks.public_frontier_requeue import count_pdf_miss_retired, requeue_pdf_miss_retired, undo_requeue_run
from scraper.tasks.public_pipeline import PublicPipeline
from tests.fixtures import judgment_html


def _pdf_shaped_result(html: str, *, url: str, content_type: str = "application/pdf") -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content=html.encode("utf-8"),
        content_type=content_type,
    )


@pytest.mark.asyncio
async def test_html_judgment_accepted_from_pdf_shaped_url(db, source, monkeypatch):
    url = "http://127.0.0.1:9/caselaw/view-file/TOKEN"
    body = judgment_html("PLD 1980 Kar 101", title="Alpha versus Beta")
    body = body.replace("</body>", "<p>" + ("Further reasons and analysis. " * 40) + "</p></body>")
    assert html_judgment_word_count(body) >= 200
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={
            "kind": "judgment",
            "url": url,
            "route": {"pdf_endpoint_kind": "view-file"},
            "meta": {"citation": "PLD 1980 Kar 101"},
        },
        cursor_json={},
        priority=50,
    )
    db.add(row)
    await db.flush()

    async def fake_fetch(_url):
        return _pdf_shaped_result(body, url=url)

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    monkeypatch.setattr(pipeline, "ingest_judgment", AsyncMock(return_value="staged"))

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "done"
    pipeline.ingest_judgment.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_whitespace_body_deferred_with_backoff(db, source, monkeypatch):
    url = "http://127.0.0.1:9/caselaw/view-file/EMPTY"
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={"kind": "judgment", "url": url, "route": {"pdf_endpoint_kind": "view-file"}},
        cursor_json={},
        priority=50,
    )
    db.add(row)
    await db.flush()

    async def fake_fetch(_url):
        return _pdf_shaped_result("   \n\t  ", url=url)

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "pending"
    assert "empty document body" in (row.last_error or "")
    assert row.next_run_at is not None
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_temporary_non_pdf_pending_with_backoff_not_retired(db, source, monkeypatch):
    url = "http://127.0.0.1:9/caselaw/view-file/BIN"
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={"kind": "judgment", "url": url, "route": {"pdf_endpoint_kind": "view-file"}},
        cursor_json={},
        priority=50,
    )
    db.add(row)
    await db.flush()

    async def fake_fetch(_url):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            content=b"not a pdf yet",
            content_type="application/pdf",
        )

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "pending"
    assert row.last_error == f"HTTP 200; {PDF_MISS_DEFER_ERROR}"
    assert row.next_run_at is not None
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_requeue_pdf_miss_retired_dry_run_and_apply_undo(db, source):
    url = "http://example.test/view-file/x"
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={"kind": "judgment", "url": url, "meta": {"citation": "PLD 1980 Kar 1"}},
        cursor_json={},
        status="retired",
        last_error="missing %PDF signature for judgment document URL",
        attempts=2,
        priority=50,
    )
    db.add(row)
    await db.commit()

    assert await count_pdf_miss_retired(db, source_name=source.source_name, year=1980) == 1
    assert await count_pdf_miss_retired(db, source_name=source.source_name, year=1999) == 0

    dry = await requeue_pdf_miss_retired(db, source_name=source.source_name, run_id="dry", year=1980, dry_run=True)
    assert dry["matched"] == 1
    await db.refresh(row)
    assert row.status == "retired"

    applied = await requeue_pdf_miss_retired(db, source_name=source.source_name, run_id="run-1", year=1980, dry_run=False)
    await db.refresh(row)
    assert row.status == "pending"
    assert row.last_error is None
    assert row.attempts == 0

    undone = await undo_requeue_run(db, "run-1")
    assert undone["restored"] == 1
    await db.refresh(row)
    assert row.status == "retired"
    assert row.attempts == 2


@pytest.mark.asyncio
async def test_empty_body_retires_only_after_bounded_retries(db, source, monkeypatch):
    url = "http://127.0.0.1:9/caselaw/view-file/EMPTY-RETRY"
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={"kind": "judgment", "url": url, "route": {"pdf_endpoint_kind": "view-file"}},
        cursor_json={},
        attempts=9,
        priority=50,
    )
    db.add(row)
    await db.flush()

    async def fake_fetch(_url):
        return _pdf_shaped_result("   ", url=url)

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "retired"
    assert row.attempts == 10
    assert "empty document body" in (row.last_error or "")
    assert "retired after 10 attempts" in (row.last_error or "")
