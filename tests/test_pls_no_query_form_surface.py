"""Regression tests for PakistanLawSite CitationSearch surface + dashboard navigation."""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select

from scraper.auth.session_manager import PageResult
from scraper.config import settings
from scraper.extractors.deterministic import introspect_search_form
from scraper.models import CrawlFrontier, SearchFormMap
from scraper.pls_navigation import (
    CitationSearchNavigationFailed,
    check_page_login_required_reason,
    citation_search_surface_is_harvestable,
    classify_pls_page,
    discover_citation_search_entrypoints,
    open_citation_search,
    open_citation_search_for_harvest,
    pls_check_url,
)
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
from scraper.tasks.search_map import map_search_form
from tests.fixtures import (
    BrowserScript,
    citation_search_archived_grid_html,
    citation_search_empty_session_shell_html,
    citation_search_grid_only_html,
    pls_check_dashboard_html,
    pls_check_dashboard_with_citation_form_html,
    pls_whats_new_table_html,
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


def test_whats_new_table_is_not_harvestable_citation_surface():
    assert not citation_search_surface_is_harvestable(pls_whats_new_table_html())
    assert introspect_search_form(pls_whats_new_table_html())["surface"] == "grid_surface_no_query_form"


def test_archived_patient_grid_is_harvestable_citation_surface():
    assert citation_search_surface_is_harvestable(citation_search_archived_grid_html())


def test_classify_citation_search_no_results_stub():
    assert classify_pls_page(citation_search_empty_session_shell_html()) == "citation_search_no_results"


def test_classify_dashboard_with_embedded_citation_form():
    assert classify_pls_page(pls_check_dashboard_with_citation_form_html(), pls_check_url()) == "dashboard_with_citation_form"


async def test_open_citation_search_rejects_whats_new_then_falls_back_to_direct(fixture_server, monkeypatch):
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_html())
    fixture_server.add("/Login/GetStatuesSearch", pls_whats_new_table_html())
    fixture_server.add("/Login/CitationSearch", citation_search_archived_grid_html())
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        pls_whats_new_table_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    sc.page(
        ("goto", settings.PLS_SEARCH_URL),
        citation_search_archived_grid_html(),
        url=settings.PLS_SEARCH_URL,
    )
    browser = await sc.factory()({}, 1)
    page = await open_citation_search(browser, archived_grid_start_row=0)
    nav = (page.metadata or {}).get("pls_citation_search_nav") or {}
    assert nav.get("discovered"), "expected diagnosable discovery metadata"
    assert citation_search_surface_is_harvestable(page.html or "")
    assert "archivedpatientGrid" in (page.html or "")
    goto_urls = [call[1] for call in browser.calls if call[0] == "goto"]
    assert goto_urls[0] == settings.PLS_CHECK_URL
    assert any("GetStatuesSearch" in url for url in goto_urls)
    assert goto_urls[-1] == settings.PLS_SEARCH_URL


async def test_direct_citation_search_grid_only_with_check_referer(fixture_server, monkeypatch):
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_html())
    fixture_server.add("/Login/GetStatuesSearch", pls_whats_new_table_html())

    def citation_search_goto(browser, goto_kwargs=None):
        referer = (goto_kwargs or {}).get("referer") or ""
        if "/Login/Check" in referer:
            return PageResult(
                url=settings.PLS_SEARCH_URL,
                html=citation_search_archived_grid_html(),
                status=200,
            )
        return PageResult(
            url=settings.PLS_SEARCH_URL,
            html=citation_search_empty_session_shell_html(),
            status=200,
        )

    sc = BrowserScript()
    sc.routes[("goto", settings.PLS_SEARCH_URL)] = citation_search_goto
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        pls_whats_new_table_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    browser = await sc.factory()({}, 1)
    page = await open_citation_search(browser, archived_grid_start_row=0)
    assert citation_search_surface_is_harvestable(page.html or "")
    direct_calls = [c for c in browser.calls if c[0] == "goto" and c[1] == settings.PLS_SEARCH_URL]
    assert any((c[3] or {}).get("referer") for c in direct_calls)


async def test_dashboard_form_submit_reaches_archived_grid(fixture_server, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2024)
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_with_citation_form_html())
    fixture_server.add("/Login/GetStatuesSearch", pls_whats_new_table_html())
    fixture_server.add_post("/Login/CitationSearch", citation_search_archived_grid_html())
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_with_citation_form_html(), url=settings.PLS_CHECK_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        pls_whats_new_table_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    sc.page(("goto", settings.PLS_SEARCH_URL), citation_search_empty_session_shell_html(), url=settings.PLS_SEARCH_URL)
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_with_citation_form_html(), url=settings.PLS_CHECK_URL)
    sc.default_search = lambda values, browser: PageResult(
        url=settings.PLS_SEARCH_URL,
        html=citation_search_archived_grid_html(),
        metadata={"pls_page_type": "citation_search_grid"},
    )
    browser = await sc.factory()({}, 1)
    page = await open_citation_search(browser, archived_grid_start_row=0)
    assert "archivedpatientGrid" in (page.html or "")
    nav = (page.metadata or {}).get("pls_citation_search_nav") or {}
    assert nav.get("via") == "dashboard_form_submit"


async def test_citation_search_form_only_triggers_form_fallback_attempt(fixture_server, monkeypatch):
    """Form-only CitationSearch (no grid) must POST warmup before failing harvest without a grid."""
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2024)
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_html())
    fixture_server.add("/Login/GetStatuesSearch", pls_whats_new_table_html())
    fixture_server.add("/Login/CitationSearch", search_form_html())
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        pls_whats_new_table_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    sc.page(("goto", settings.PLS_SEARCH_URL), search_form_html(), url=settings.PLS_SEARCH_URL)
    sc.default_search = lambda values, browser: PageResult(
        url=settings.PLS_SEARCH_URL,
        html=citation_search_empty_session_shell_html(),
    )
    browser = await sc.factory()({}, 1)
    with pytest.raises(CitationSearchNavigationFailed, match="no-results stub") as raised:
        await open_citation_search_for_harvest(browser)
    nav = ((raised.value.page.metadata if raised.value.page else {}) or {}).get("pls_citation_search_nav") or {}
    kinds = [a.get("kind") for a in nav.get("attempts") or []]
    assert "direct_referer" in kinds
    assert "citation_form" in kinds
    assert any(isinstance(c[0], tuple) and c[0][0] == "search" for c in browser.calls)


async def test_open_citation_search_for_harvest_rejects_no_results_stub(fixture_server, monkeypatch):
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_html())
    fixture_server.add("/Login/GetStatuesSearch", pls_whats_new_table_html())
    fixture_server.add("/Login/CitationSearch", citation_search_empty_session_shell_html())
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        pls_whats_new_table_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    sc.page(("goto", settings.PLS_SEARCH_URL), citation_search_empty_session_shell_html(), url=settings.PLS_SEARCH_URL)
    browser = await sc.factory()({}, 1)
    with pytest.raises(CitationSearchNavigationFailed, match="no-results stub"):
        await open_citation_search_for_harvest(browser)


async def test_map_non_grid_page_does_not_create_search_map(db, login_source):
    from scraper.auth.session_manager import LoginRequired

    before = (await db.execute(select(func.count()).select_from(SearchFormMap))).scalar()
    with pytest.raises(LoginRequired):
        await map_search_form(db, login_source, pls_whats_new_table_html())
    after = (await db.execute(select(func.count()).select_from(SearchFormMap))).scalar()
    assert after == before


async def test_pipeline_bare_fragment_with_valid_check_stale_not_login(db, login_source, fixture_server, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2020)
    _wire_pls_urls(fixture_server, monkeypatch)
    fixture_server.add("/Login/Check", pls_check_dashboard_html())
    fixture_server.add("/Login/GetStatuesSearch", pls_whats_new_table_html())
    fixture_server.add("/Login/CitationSearch", citation_search_archived_grid_html())
    await _activate(db, login_source)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        pls_whats_new_table_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    sc.page(
        ("goto", settings.PLS_SEARCH_URL),
        citation_search_archived_grid_html(),
        url=settings.PLS_SEARCH_URL,
    )
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await pipeline.run(max_queries=1, max_probes_per_volume=1)
    await db.commit()
    await r.aclose()
    maps = (await db.execute(select(SearchFormMap))).scalars().all()
    assert maps
    assert any("archivedpatientgrid" in str((m.result_layout or {}).get("row_selector") or "").lower() for m in maps)
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
    fixture_server.add("/Login/GetStatuesSearch", pls_whats_new_table_html())
    fixture_server.add("/Login/CitationSearch", citation_search_empty_session_shell_html())
    await _activate(db, login_source)
    db.add(
        SearchFormMap(
            source_name=login_source.source_name,
            map_version=1,
            fields={},
            result_layout={"row_selector": "table tr"},
            page_size=None,
            pagination={},
            detail_layout={},
            limits={"surface": "no_query_form"},
            dom_hash="seeded-no-query",
            mapped_by="test",
            verified_against_dom=True,
            is_active=True,
            stale=True,
        )
    )
    await db.flush()
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_CHECK_URL), pls_check_dashboard_html(), url=settings.PLS_CHECK_URL)
    sc.page(
        ("goto", fixture_server.url("/Login/GetStatuesSearch")),
        pls_whats_new_table_html(),
        url=fixture_server.url("/Login/GetStatuesSearch"),
    )
    sc.page(
        ("goto", settings.PLS_SEARCH_URL),
        citation_search_empty_session_shell_html(),
        url=settings.PLS_SEARCH_URL,
    )
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
