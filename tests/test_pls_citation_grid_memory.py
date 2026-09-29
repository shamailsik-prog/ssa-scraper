"""PLS citation-grid harvest must not retain every window's HTML across fast reload loops."""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis

from scraper.auth.session_manager import PageResult, release_page_result, trim_pls_citation_search_nav
from scraper.config import settings
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
from tests.fixtures import BrowserScript, judgment_html
from tests.test_auth_playwright import _activate, _archived_grid_html, _nosleep


def test_release_page_result_clears_heavy_fields():
    page = PageResult(
        url="https://example.test/",
        html="x" * 50_000,
        metadata={
            "case_description_modal_text": "modal " * 2000,
            "pls_citation_search_nav": {"attempts": [{"url": "u"}], "discovered": [{"url": "d"}]},
            "start_row": 3,
        },
    )
    release_page_result(page)
    assert page.html == ""
    assert page.pdf_bytes is None
    assert "case_description_modal_text" not in page.metadata
    assert "pls_citation_search_nav" not in page.metadata
    assert page.metadata.get("start_row") == 3


def test_trim_pls_citation_search_nav_caps_attempts():
    nav = {"attempts": [{"n": i} for i in range(100)], "discovered": [{"u": i} for i in range(50)]}
    slim = trim_pls_citation_search_nav(nav)
    assert len(slim["attempts"]) == 24
    assert slim.get("attempts_truncated") == 100
    assert len(slim["discovered"]) == 32
    assert slim.get("discovered_truncated") == 50


@pytest.mark.asyncio
async def test_citation_grid_multi_window_run_releases_grid_pages(db, login_source, monkeypatch):
    from scraper.harvest_mode import set_harvest_mode

    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 0)
    monkeypatch.setattr(settings, "BACKFILL_PLS_CITATION_GRID_MAX_DETAIL", 1)
    monkeypatch.setattr(settings, "BACKFILL_PLS_CITATION_GRID_SCAN_WINDOW", 1)
    monkeypatch.setattr(settings, "BACKFILL_PLS_RUN_MAX_MINUTES", 30)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="memory-test")
    await _activate(db, login_source)

    all_rows = [
        (f"PLD 2024 SC {n}", f"Case {n}", "Supreme Court", f"https://www.pakistanlawsite.com/case/{n}")
        for n in range(5001, 5007)
    ]
    sc = BrowserScript()

    def grid(browser, goto_kwargs=None):
        start = int((goto_kwargs or {}).get("archived_grid_start_row", 0) or 0)
        window = all_rows[start : start + 1]
        return PageResult(
            url=settings.PLS_SEARCH_URL,
            html=_archived_grid_html(window),
            status=200,
            metadata={
                "total_rows": len(all_rows),
                "start_row": start,
                "requested_start_row": start,
                "seek_mode": "dom_absolute",
            },
        )

    sc.routes[("goto", settings.PLS_SEARCH_URL)] = grid
    for citation, title, _court, detail_url in all_rows:
        sc.page(("goto", detail_url), judgment_html(citation, title=title))

    retained_html_chars = []

    def track_release(page):
        if page is not None and page.html:
            retained_html_chars.append(len(page.html))
        release_page_result(page)

    monkeypatch.setattr("scraper.tasks.pakistanlawsite.release_page_result", track_release)

    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=5, max_probes_per_volume=5)
    await db.commit()
    await r.aclose()

    assert stats["citation_grid_windows"] >= 3
    assert retained_html_chars, "expected grid/detail pages to be released with non-empty html first"
    assert max(retained_html_chars) < 200_000
