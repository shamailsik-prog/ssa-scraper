"""Regression tests for PakistanLawSite CitationSearch surface classification (no form / login shell)."""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select

from scraper.auth.session_manager import LoginRequired
from scraper.config import settings
from scraper.extractors.deterministic import introspect_search_form
from scraper.models import CrawlFrontier, Notification, SearchFormMap
from scraper.tasks.login_recovery import looks_authenticated
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
from scraper.tasks.search_map import map_search_form
from tests.fixtures import (
    BrowserScript,
    citation_search_empty_session_shell_html,
    citation_search_grid_only_html,
    citation_search_no_query_form_html,
    search_form_html,
)
from tests.test_auth_playwright import _activate, _nosleep


class _Page:
    def __init__(self, html: str, url: str = "https://www.pakistanlawsite.com/Login/CitationSearch"):
        self.html = html
        self.url = url


def test_empty_citation_search_shell_is_login_required():
    probe = introspect_search_form(citation_search_empty_session_shell_html())
    assert probe["surface"] == "login_required"
    assert probe["fields"] == []


def test_keepalive_rejects_empty_citation_search_shell():
    page = _Page(citation_search_empty_session_shell_html())
    assert looks_authenticated(page) is False


def test_logged_in_grid_surface_classification_unchanged():
    probe = introspect_search_form(citation_search_grid_only_html())
    assert probe["surface"] == "grid_surface_no_query_form"
    assert probe["fields"] == []


async def test_map_login_shell_marks_stale_with_notification(db, login_source):
    m = await map_search_form(db, login_source, citation_search_empty_session_shell_html())
    assert m.stale is True
    assert (m.limits or {}).get("surface") == "login_required"
    codes = (await db.execute(select(Notification.code))).scalars().all()
    assert "SEARCH_MAP_NO_QUERY_FORM_STALE" in codes


async def test_pipeline_login_shell_raises_without_retiring_frontiers(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2020)
    await _activate(db, login_source)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), citation_search_empty_session_shell_html())
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    with pytest.raises(LoginRequired):
        await pipeline.run(max_queries=5, max_probes_per_volume=1)
    await db.commit()
    await r.aclose()
    retired = (
        await db.execute(select(func.count()).select_from(CrawlFrontier).where(CrawlFrontier.status == "retired"))
    ).scalar()
    assert retired == 0


async def test_no_query_form_frontier_marked_stale_not_retired(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2020)
    monkeypatch.setattr(settings, "VOLUME_END_GAP", 40)
    await _activate(db, login_source)
    await map_search_form(db, login_source, citation_search_no_query_form_html())
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), citation_search_no_query_form_html())
    sc.default_search = lambda values, browser: _Page(search_form_html(), "https://www.pakistanlawsite.com/r")
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await pipeline.run(max_queries=1, max_probes_per_volume=1)
    await db.commit()
    await r.aclose()
    stale_rows = (
        await db.execute(select(CrawlFrontier).where(CrawlFrontier.status == "stale"))
    ).scalars().all()
    assert stale_rows, "expected at least one frontier row marked stale"
    assert any("SEARCH_MAP_NO_QUERY_FORM_STALE" in (fr.last_error or "") for fr in stale_rows)
    retired = (
        await db.execute(select(func.count()).select_from(CrawlFrontier).where(CrawlFrontier.status == "retired"))
    ).scalar()
    assert retired == 0
    active = (await db.execute(select(SearchFormMap).where(SearchFormMap.is_active.is_(True)))).scalars().first()
    assert active is not None and active.stale is True


async def test_query_form_surface_still_maps_and_runs(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 0)
    await _activate(db, login_source)
    m = await map_search_form(db, login_source, search_form_html())
    assert m.stale is False
    assert (m.limits or {}).get("surface") == "query_form"
