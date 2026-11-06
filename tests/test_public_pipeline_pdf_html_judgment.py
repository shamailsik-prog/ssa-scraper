"""PDF-shaped judgment URLs that return HTML or temporary non-PDF bodies."""

from unittest.mock import AsyncMock

import pytest

from scraper.fetchers import FetchResult, HttpFetcher
from scraper.judgment_document_body import PDF_MISS_DEFER_ERROR, html_judgment_word_count
from sqlalchemy import select

from scraper.models import CorpusMetadata, CrawlFrontier
from scraper.tasks.public_frontier_requeue import LATEST_RUN_KEY
from scraper.tasks.public_frontier_requeue import count_pdf_miss_retired, requeue_pdf_miss_retired, undo_requeue_run
from scraper.tasks.public_pipeline import PublicPipeline
from tests.fixtures import judgment_html
from tests.test_judgment_document_body import _shc_order_sheet_word_html


def _pdf_shaped_result(html: str, *, url: str, content_type: str = "application/pdf") -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content=html.encode("utf-8"),
        content_type=content_type,
    )


@pytest.mark.asyncio
async def test_shc_order_sheet_word_html_ingested_from_pdf_shaped_url(db, source, monkeypatch):
    url = "http://127.0.0.1:9/caselaw/view-file/SHC-ORDER-SHEET"
    body = _shc_order_sheet_word_html()
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={
            "kind": "judgment",
            "url": url,
            "route": {"pdf_endpoint_kind": "view-file"},
            "meta": {"citation": "2024 SHC KHI 1234"},
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
async def test_empty_whitespace_three_times_within_24h_stays_pending(db, source, monkeypatch):
    url = "http://127.0.0.1:9/caselaw/view-file/EMPTY-BURST"
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

    for expected_attempts in (1, 2, 3):
        assert await pipeline._drain_one(row) == "ok"
        assert row.status == "pending"
        assert row.attempts == expected_attempts

    assert row.cursor_json.get("empty_body_first_seen_at")


@pytest.mark.asyncio
async def test_empty_whitespace_body_retires_after_three_attempts_and_24h(db, source, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from scraper.judgment_document_body import EMPTY_BODY_FIRST_SEEN_AT_KEY

    url = "http://127.0.0.1:9/caselaw/view-file/EMPTY-RETIRE"
    first_seen = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"judgment:{url}",
        query_json={"kind": "judgment", "url": url, "route": {"pdf_endpoint_kind": "view-file"}},
        cursor_json={EMPTY_BODY_FIRST_SEEN_AT_KEY: first_seen},
        priority=50,
        attempts=2,
    )
    db.add(row)
    await db.flush()

    async def fake_fetch(_url):
        return _pdf_shaped_result("   \n\t  ", url=url)

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "retired"
    assert row.attempts == 3
    assert "empty document body" in (row.last_error or "")
    assert "retired after 3 attempts" in (row.last_error or "")


@pytest.mark.asyncio
async def test_empty_whitespace_body_pending_with_backoff(db, source, monkeypatch):
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
    assert "HTTP 200" in (row.last_error or "")
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

    assert await count_pdf_miss_retired(db, source_name=source.source_name) == 1
    assert await count_pdf_miss_retired(db, source_name=source.source_name, year=1980) == 1
    assert await count_pdf_miss_retired(db, source_name=source.source_name, year=1999) == 0

    dry = await requeue_pdf_miss_retired(db, source_name=source.source_name, run_id="dry", dry_run=True)
    assert dry["matched"] == 1
    await db.refresh(row)
    assert row.status == "retired"

    applied = await requeue_pdf_miss_retired(db, source_name=source.source_name, run_id="run-1", dry_run=False)
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
async def test_requeue_pdf_miss_retired_limit_pilot_slice(db, source):
    for idx in range(3):
        db.add(
            CrawlFrontier(
                source_name=source.source_name,
                tier=0,
                query_key=f"judgment:http://example.test/pilot-{idx}",
                query_json={"kind": "judgment", "url": f"http://example.test/pilot-{idx}"},
                cursor_json={},
                status="retired",
                last_error="missing %PDF signature for judgment document URL",
                priority=50,
            )
        )
    await db.commit()

    assert await count_pdf_miss_retired(db, source_name=source.source_name) == 3
    assert await count_pdf_miss_retired(db, source_name=source.source_name, limit=2) == 2

    pilot = await requeue_pdf_miss_retired(
        db, source_name=source.source_name, run_id="pilot", limit=2, dry_run=True
    )
    assert pilot["matched"] == 2


@pytest.mark.asyncio
async def test_requeue_apply_twice_upserts_latest_metadata_without_pk_error(db, source):
    for idx in (1, 2):
        db.add(
            CrawlFrontier(
                source_name=source.source_name,
                tier=0,
                query_key=f"judgment:http://example.test/twice-{idx}",
                query_json={"kind": "judgment", "url": f"http://example.test/twice-{idx}"},
                cursor_json={},
                status="retired",
                last_error="missing %PDF signature for judgment document URL",
                attempts=1,
                priority=50,
            )
        )
    await db.commit()

    first = await requeue_pdf_miss_retired(
        db, source_name=source.source_name, run_id="run-a", limit=1, dry_run=False
    )
    second = await requeue_pdf_miss_retired(db, source_name=source.source_name, run_id="run-b", dry_run=False)
    assert first["matched"] == 1
    assert second["matched"] == 1
    latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
    assert latest is not None
    assert latest.value == "run-b"


@pytest.mark.asyncio
async def test_undo_skips_rows_already_progressed_by_worker(db, source):
    for label in ("fresh", "done"):
        db.add(
            CrawlFrontier(
                source_name=source.source_name,
                tier=0,
                query_key=f"judgment:http://example.test/{label}",
                query_json={"kind": "judgment", "url": f"http://example.test/{label}"},
                cursor_json={},
                status="retired",
                last_error="missing %PDF signature for judgment document URL",
                attempts=2,
                priority=50,
            )
        )
    await db.commit()

    applied = await requeue_pdf_miss_retired(db, source_name=source.source_name, run_id="run-progress", dry_run=False)
    assert applied["matched"] == 2

    progressed = (
        await db.execute(select(CrawlFrontier).where(CrawlFrontier.query_key == "judgment:http://example.test/done"))
    ).scalars().first()
    progressed.status = "done"
    progressed.attempts = 1
    progressed.last_error = None
    await db.commit()

    undone = await undo_requeue_run(db, "run-progress")
    assert undone["restored"] == 1
    assert undone["skipped"] == 1

    fresh = (
        await db.execute(select(CrawlFrontier).where(CrawlFrontier.query_key == "judgment:http://example.test/fresh"))
    ).scalars().first()
    done = (
        await db.execute(select(CrawlFrontier).where(CrawlFrontier.query_key == "judgment:http://example.test/done"))
    ).scalars().first()
    assert fresh.status == "retired"
    assert done.status == "done"


@pytest.mark.asyncio
async def test_http_503_retires_after_ten_attempts(db, source, monkeypatch):
    url = "http://127.0.0.1:9/caselaw/view-file/503"
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
            status_code=503,
            content=b"service unavailable",
            content_type="text/html",
        )

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)

    for _ in range(9):
        assert await pipeline._drain_one(row) == "ok"
        assert row.status == "pending"
        assert row.last_error == "HTTP 503"
        assert row.attempts == _ + 1

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "retired"
    assert row.last_error == "HTTP 503 (retired after 10 attempts)"
    assert row.attempts == 10


@pytest.mark.asyncio
async def test_long_nav_html_without_judgment_marker_not_ingested(db, source, monkeypatch):
    url = "http://127.0.0.1:9/caselaw/view-file/NAV"
    filler = " ".join(["Home About Contact Search Results Pagination Browse Archive"] * 40)
    body = f"<html><body><div class='site-chrome'>{filler}</div></body></html>"
    assert html_judgment_word_count(body) >= 300
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
        return _pdf_shaped_result(body, url=url)

    pipeline = PublicPipeline(db, source, fetcher=None)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    monkeypatch.setattr(pipeline, "ingest_judgment", AsyncMock(return_value="staged"))

    assert await pipeline._drain_one(row) == "ok"
    assert row.status == "pending"
    pipeline.ingest_judgment.assert_not_awaited()
    assert PDF_MISS_DEFER_ERROR in (row.last_error or "")


@pytest.mark.asyncio
async def test_pdf_header_html_judgment_records_html_not_original_pdf(db, source):
    """An accepted HTML judgment with application/pdf must not become a fake original PDF."""
    from scraper.fetchers import read_raw
    from scraper.judgment_document_body import classify_pdf_endpoint_body
    from scraper.models import ScraperStaging, SourceProvenance

    url = "http://127.0.0.1:9/caselaw/view-file/HTML-MISLABEL-PROVENANCE"
    html = judgment_html("PLD 2026 SC 987654", title="Provenance Alpha versus Beta")
    html = html.replace("</body>", "<p>" + ("Further reasons and analysis. " * 45) + "</p></body>")
    response = _pdf_shaped_result(html, url=url, content_type="application/pdf")
    assert response.is_pdf and response.content_kind == "pdf"
    assert classify_pdf_endpoint_body(response) == "html_judgment"

    pipeline = PublicPipeline(db, source, fetcher=None)
    await pipeline.ingest_judgment(response, route={"frontier": "mislabel-provenance-test"})

    prov = (await db.execute(
        select(SourceProvenance).where(SourceProvenance.content_hash == response.content_hash)
    )).scalars().one()
    assert prov.content_kind == "html"
    assert prov.document_kind == "html_text"
    assert prov.is_original_document is False
    assert read_raw(prov.raw_ref) == response.content
    staging = (await db.execute(
        select(ScraperStaging).where(ScraperStaging.provenance_id == prov.id)
    )).scalars().one()
    assert staging.pdf_provenance_id is None
