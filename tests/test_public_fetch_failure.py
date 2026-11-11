"""A public run that fetches nothing because TLS or transport failed is not a success.

PakistanCode's robots.txt request raises before any page is counted. The frontier row was
deferred and the job was stored as done, last_error cleared, and last_success_at moved.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from scraper.fetchers import FetchResult, HttpFetcher
from scraper.models import ScraperJob, ScraperSource
from scraper.security import RobotsUnavailable
from scraper.tasks.dispatcher import run_source
from scraper.tasks.pakistancode import scrape_pakistancode
from scraper.tasks.public_pipeline import describe_public_fetch_failure, run_public_source

PREVIOUS_SUCCESS = datetime(2020, 1, 1, tzinfo=timezone.utc)


class _Boom:
    def __init__(self, exc: BaseException):
        self.exc = exc

    async def get(self, url, **kwargs):
        raise self.exc


def test_describe_public_fetch_failure_names_tls_and_zero_page_runs():
    assert describe_public_fetch_failure({"fetched": 3, "errors": 0, "deferred": 1, "halted": False}) is None
    assert describe_public_fetch_failure({"fetched": 0, "errors": 0, "deferred": 0, "listings_armed": 0, "halted": False}) is None
    tls = describe_public_fetch_failure(
        {
            "fetched": 0,
            "errors": 4,
            "deferred": 0,
            "halted": False,
            "last_fetch_error": "unable to get local issuer certificate",
        }
    )
    assert tls is not None and tls.startswith("fetch failed:") and "issuer" in tls
    deferred = describe_public_fetch_failure(
        {"fetched": 0, "errors": 0, "deferred": 4, "halted": False, "last_fetch_error": "robots.txt unavailable"}
    )
    assert deferred is not None and "0 pages" in deferred
    partial = describe_public_fetch_failure({"fetched": 2, "errors": 1, "halted": False})
    assert partial is not None and partial.startswith("fetch degraded:")
    partial_robots = describe_public_fetch_failure(
        {
            "fetched": 3,
            "errors": 0,
            "deferred": 1,
            "halted": False,
            "last_fetch_error": "robots.txt unavailable for https://pakistancode.gov.pk (HTTP 599)",
        }
    )
    assert partial_robots is not None and partial_robots.startswith("fetch degraded:") and "deferred fetch error" in partial_robots
    assert describe_public_fetch_failure({"fetched": 0, "errors": 3, "halted": True}) is None


async def test_tls_error_does_not_move_last_success(db, source, fixture_server):
    source.last_success_at = PREVIOUS_SUCCESS
    source.last_error = None
    await db.commit()
    stats = await run_public_source(
        db,
        source,
        seed_listings=[{"url": fixture_server.url("/tls"), "target_kind": "judgment"}],
        fetcher=_Boom(RuntimeError("unable to get local issuer certificate")),
        limit=5,
    )
    assert stats["fetch_failure"] is True
    assert stats["fetched"] == 0
    assert stats["errors"] == 1
    assert source.last_success_at == PREVIOUS_SUCCESS
    assert "issuer" in (source.last_error or "")
    assert "0 pages" in source.last_error


class _PageThenRobots:
    """First GET returns a page. Later GETs are the robots/TLS deferral."""

    def __init__(self):
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                content=b"<html><body>no links</body></html>",
                content_type="text/html",
            )
        raise RobotsUnavailable("robots.txt unavailable for https://pakistancode.gov.pk (HTTP 599)")


async def test_robots_unavailable_after_a_page_is_degraded(db, source, fixture_server):
    source.last_success_at = PREVIOUS_SUCCESS
    source.last_error = None
    await db.commit()
    stats = await run_public_source(
        db,
        source,
        seed_listings=[
            {"url": fixture_server.url("/ok-then-robots"), "target_kind": "judgment"},
            {"url": fixture_server.url("/robots-after"), "target_kind": "judgment"},
        ],
        fetcher=_PageThenRobots(),
        limit=5,
    )
    assert stats["fetched"] == 1
    assert stats["deferred"] == 1
    assert stats["errors"] == 0
    assert stats["fetch_failure"] is True
    assert str(stats.get("stop_reason") or "").startswith("fetch degraded:")
    assert source.last_success_at == PREVIOUS_SUCCESS
    assert "robots.txt unavailable" in (source.last_error or "")


async def test_robots_transport_failure_with_zero_pages_is_a_failure(db, source, fixture_server):
    source.last_success_at = PREVIOUS_SUCCESS
    await db.commit()
    stats = await run_public_source(
        db,
        source,
        seed_listings=[{"url": fixture_server.url("/robots-down"), "target_kind": "judgment"}],
        fetcher=_Boom(RobotsUnavailable("robots.txt unavailable for https://pakistancode.gov.pk (HTTP 599)")),
        limit=5,
    )
    assert stats["fetched"] == 0
    assert stats["deferred"] == 1
    assert stats["fetch_failure"] is True
    assert source.last_success_at == PREVIOUS_SUCCESS
    assert "deferred" in (source.last_error or "")


async def test_pakistancode_pipeline_reports_the_same_failure(db, fixture_server):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))
    ).scalars().one()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.last_success_at = PREVIOUS_SUCCESS
    source.last_error = None
    source.config_json = {"listings": [fixture_server.url("/pc")]}
    await db.commit()
    stats = await scrape_pakistancode(
        source,
        db,
        fetcher=_Boom(RuntimeError("unable to get local issuer certificate")),
        limit=5,
    )
    assert stats["fetch_failure"] is True
    assert source.last_success_at == PREVIOUS_SUCCESS
    assert "issuer" in (source.last_error or "")


async def test_a_real_page_still_counts_as_success(db, source, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/ok", "<html><body>no links</body></html>")
    source.last_success_at = None
    await db.commit()
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await run_public_source(
            db,
            source,
            seed_listings=[{"url": fixture_server.url("/ok"), "target_kind": "judgment"}],
            fetcher=fetcher,
            limit=5,
        )
    assert stats.get("fetch_failure") is not True
    assert stats["fetched"] >= 1
    assert source.last_success_at is not None
    assert source.last_success_at > PREVIOUS_SUCCESS


async def test_empty_frontier_without_seeds_is_still_a_success(db, source):
    source.last_success_at = None
    await db.commit()
    stats = await run_public_source(db, source, seed_listings=[], fetcher=_Boom(RuntimeError("unused")), limit=5)
    assert stats.get("fetch_failure") is not True
    assert stats["fetched"] == 0
    assert source.last_success_at is not None


async def _job_for(db, source_name: str) -> ScraperJob:
    return (
        await db.execute(
            select(ScraperJob)
            .where(ScraperJob.source_name == source_name)
            .order_by(ScraperJob.created_at.desc())
        )
    ).scalars().first()


async def test_dispatcher_fails_a_zero_page_fetch_and_does_not_count_last_success(db, monkeypatch):
    from scraper.tasks import dispatcher

    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))
    ).scalars().one()
    source.last_success_at = PREVIOUS_SUCCESS
    source.last_error = None
    await db.commit()

    async def fake_connector_for(_source):
        async def fake(_source_obj, _db, **_kwargs):
            return {
                "fetched": 0,
                "staged": 0,
                "errors": 4,
                "fetch_failure": True,
                "stop_reason": "fetch failed: 4 error(s), 0 pages: unable to get local issuer certificate",
                "halted": False,
            }

        return fake

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    await run_source("PakistanCode")
    await db.refresh(source)
    job = await _job_for(db, "PakistanCode")
    assert job.status == "failed"
    assert "0 pages" in (job.error_message or "")
    assert source.last_success_at == PREVIOUS_SUCCESS
    assert "issuer" in (source.last_error or "")


async def test_dispatcher_marks_a_partial_fetch_degraded(db, monkeypatch):
    from scraper.tasks import dispatcher

    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))
    ).scalars().one()
    source.last_success_at = PREVIOUS_SUCCESS
    await db.commit()

    async def fake_connector_for(_source):
        async def fake(_source_obj, _db, **_kwargs):
            return {
                "fetched": 2,
                "staged": 1,
                "errors": 1,
                "fetch_failure": True,
                "stop_reason": "fetch degraded: 1 error(s) after 2 page(s)",
                "halted": False,
            }

        return fake

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    await run_source("PakistanCode")
    await db.refresh(source)
    job = await _job_for(db, "PakistanCode")
    assert job.status == "degraded"
    assert job.pages_scraped == 2
    assert source.last_success_at == PREVIOUS_SUCCESS
    assert source.last_error


async def test_dispatcher_clears_last_error_after_a_clean_run(db, monkeypatch):
    from scraper.tasks import dispatcher

    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))
    ).scalars().one()
    source.last_error = "fetch failed: 4 error(s), 0 pages"
    source.last_success_at = PREVIOUS_SUCCESS
    await db.commit()

    async def fake_connector_for(_source):
        async def fake(_source_obj, _db, **_kwargs):
            return {"fetched": 4, "staged": 1, "errors": 0, "halted": False}

        return fake

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    await run_source("PakistanCode")
    await db.refresh(source)
    job = await _job_for(db, "PakistanCode")
    assert job.status == "done"
    assert source.last_error is None
    assert source.last_success_at == PREVIOUS_SUCCESS
