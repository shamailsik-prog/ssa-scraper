"""A public source that ends a run with work still queued starts its next run at once.

On 8 October 2026 PakistanCode processed about 130 laws per run and then waited a full cadence
(an hour) with 1,460 laws still in its frontier, so the server sat idle most of each hour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scraper.config import settings
from scraper.fetchers import HttpFetcher
from scraper.tasks.public_pipeline import run_public_source

FAR = datetime(2099, 1, 1, tzinfo=timezone.utc)


async def _run(db, source, fixture_server, paths, limit):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    source.next_scrape_at = FAR
    await db.commit()
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await run_public_source(
            db,
            source,
            seed_listings=[{"url": fixture_server.url(p), "target_kind": "judgment"} for p in paths],
            fetcher=fetcher,
            limit=limit,
        )
    await db.commit()
    return stats


async def test_run_that_stops_at_its_limit_with_work_left_is_due_again_at_once(db, source, fixture_server):
    for p in ("/l1", "/l2", "/l3"):
        fixture_server.add(p, "<html><body>no links</body></html>")
    before = datetime.now(timezone.utc)
    stats = await _run(db, source, fixture_server, ["/l1", "/l2", "/l3"], limit=2)
    assert stats["backlog_remaining"] is True
    assert source.next_scrape_at <= before + timedelta(seconds=settings.PUBLIC_BACKLOG_CONTINUE_SECONDS + 5)


async def test_run_that_empties_its_frontier_keeps_the_cadence(db, source, fixture_server):
    for p in ("/l1", "/l2"):
        fixture_server.add(p, "<html><body>no links</body></html>")
    stats = await _run(db, source, fixture_server, ["/l1", "/l2"], limit=10)
    assert stats["backlog_remaining"] is False
    assert source.next_scrape_at == FAR


async def test_run_that_completed_nothing_does_not_spin(db, source, fixture_server):
    """A server error leaves the row pending: rerunning every minute would only hammer the site."""
    fixture_server.add("/down1", "<html>err</html>", status=503)
    fixture_server.add("/down2", "<html>err</html>", status=503)
    stats = await _run(db, source, fixture_server, ["/down1", "/down2"], limit=1)
    assert stats["backlog_remaining"] is False
    assert source.next_scrape_at == FAR
