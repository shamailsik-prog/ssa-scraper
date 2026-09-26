"""Regression tests for PakistanLawSite CitationSearch surface + dashboard navigation."""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select

from scraper.auth.session_manager import PageResult
from scraper.config import settings
from scraper.extractors.deterministic import introspect_search_form
from scraper.models import CrawlFrontier, Notification, SearchFormMap
from scraper.pls_navigation import (
    check_page_login_required_reason,
    discover_citation_search_entrypoints,
    open_citation_search,
    pls_check_url,
)
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
from scraper.tasks.search_map import map_search_form
from tests.fixtures import (
    BrowserScript,
    citation_search_empty_session_shell_html,
    citation_search_grid_only_html,
    citation_search_no_query_form_html,
    pls_check_dashboard_html,
    search_form_html,
)
from tests.test_auth_playwright import _activate, _nosleep


def _wire_pls_urls(fixture_server, monkeypatch) -> None:
    base = fixture_server.url("").rstrip("/")
    monkeypatch.setattr(settings, "PLS_BASE_URL", base)
    monkeypatch.setattr(settings, "PLS_CHECK_URL", fixture_server.url("/Login/Check"))
    monkeypatch.setattr(settings, "PLS_SEARCH_URL", fixture_server.url("/Login/CitationSearch"))


def test_bare_citation_fragment_is_no_query_form_not_login_required():
    probe = introspect_search_form(citation_search_empty_session_shell_html())
    assert probe["surface"] == "no_query_form"
    assert probe["fields"] == []


def test_check_page_valid_when_logout_present():
    assert check_page_login_required_reason(pls_check_dashboard_html(), pls_check_url()) is None


def test_check_page_login_required_without_logout():
    assert check_page_login_required_reason("<html><body><form><input type=password name=x></form></body></html>", pls_check_url())


def test_discover_dashboard_ajax_entrypoint():
    found = discover_citation_search_entrypoints(pls_check_dashboard_html(), "https://www.pakistanlawsite.com")
    kinds = {item["kind"] for item in found}
    urls = " ".join(item["url"] for item in found).lower()
    assert "getstatuessearch" in urls
    assert "ajax" in kinds or "link" in kinds


def test_logged_in_grid_surface_classification_unchanged():
    probe = introspect_search_form(citation_search_grid_only_html())
    assert probe["surface"] == "grid_surface_no_query_form"
    assert probe["fields"] == []


async def test_open_citation_search_uses_check_then_ajax_not_bare_direct(fixture_server, monkeypatch):
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_html())
    fixture_server.add("/Login/GetStatuesSearch", search_form_html())
    fixture_server.add("/Login/CitationSearch", citation_search_empty_session_shell_html())
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(("goto", settings.PLS_SEARCH_URL), citation_search_empty_session_shell_html(), url=settings.PLS_SEARCH_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        search_form_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    browser = await sc.factory()({}, 1)
    page = await open_citation_search(browser, archived_grid_start_row=0)
    nav = (page.metadata or {}).get("pls_citation_search_nav") or {}
    assert nav.get("discovered"), "expected diagnosable discovery metadata"
    assert introspect_search_form(page.html)["surface"] == "query_form"
    goto_urls = [call[1] for call in browser.calls if call[0] == "goto"]
    assert goto_urls[0] == settings.PLS_CHECK_URL
    assert any("GetStatuesSearch" in url for url in goto_urls)


async def test_map_bare_fragment_marks_stale_with_notification(db, login_source):
    m = await map_search_form(db, login_source, citation_search_empty_session_shell_html())
    assert m.stale is True
    assert (m.limits or {}).get("surface") == "no_query_form"
    codes = (await db.execute(select(Notification.code))).scalars().all()
    assert "SEARCH_MAP_NO_QUERY_FORM_STALE" in codes


async def test_pipeline_bare_fragment_with_valid_check_stale_not_login(db, login_source, fixture_server, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2020)
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_html())
    fixture_server.add("/Login/GetStatuesSearch", citation_search_empty_session_shell_html())
    fixture_server.add("/Login/CitationSearch", citation_search_empty_session_shell_html())
    await _activate(db, login_source)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(("goto", settings.PLS_SEARCH_URL), citation_search_empty_session_shell_html(), url=settings.PLS_SEARCH_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        citation_search_empty_session_shell_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await pipeline.run(max_queries=1, max_probes_per_volume=1)
    await db.commit()
    await r.aclose()
    stale_rows = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.status == "stale"))).scalars().all()
    assert stale_rows
    assert any("SEARCH_MAP_NO_QUERY_FORM_STALE" in (fr.last_error or "") for fr in stale_rows)
    retired = (
        await db.execute(select(func.count()).select_from(CrawlFrontier).where(CrawlFrontier.status == "retired"))
    ).scalar()
    assert retired == 0


async def test_no_query_form_frontier_marked_stale_not_retired(db, login_source, fixture_server, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2020)
    monkeypatch.setattr(settings, "VOLUME_END_GAP", 40)
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_html())
    fixture_server.add("/Login/CitationSearch", citation_search_no_query_form_html())
    await _activate(db, login_source)
    await map_search_form(db, login_source, citation_search_no_query_form_html())
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(("goto", settings.PLS_SEARCH_URL), citation_search_no_query_form_html(), url=settings.PLS_SEARCH_URL)
    sc.default_search = lambda values, browser: PageResult(url="https://www.pakistanlawsite.com/r", html=search_form_html())
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await pipeline.run(max_queries=1, max_probes_per_volume=1)
    await db.commit()
    await r.aclose()
    stale_rows = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.status == "stale"))).scalars().all()
    assert stale_rows
    assert any("SEARCH_MAP_NO_QUERY_FORM_STALE" in (fr.last_error or "") for fr in stale_rows)
    retired = (
        await db.execute(select(func.count()).select_from(CrawlFrontier).where(CrawlFrontier.status == "retired"))
    ).scalar()
    assert retired == 0


async def test_query_form_surface_still_maps_and_runs(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 0)
    await _activate(db, login_source)
    m = await map_search_form(db, login_source, search_form_html())
    assert m.stale is False
    assert (m.limits or {}).get("surface") == "query_form"
