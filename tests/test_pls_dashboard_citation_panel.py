"""Dashboard Citation Search panel must be visible before Playwright fills #Citation_* fields."""

import pytest

from scraper.extractors.deterministic import search_map_uses_dashboard_citation_panel


def test_search_map_uses_dashboard_citation_panel_detects_controls():
    assert search_map_uses_dashboard_citation_panel(
        {
            "fields": {
                "reporter": {"selector": "#Citation_Category_Search_dropdown"},
                "year": {"selector": "#Citation_Year_Search_input"},
            }
        }
    )


def test_search_map_uses_dashboard_citation_panel_ignores_grid_only():
    assert not search_map_uses_dashboard_citation_panel(
        {"fields": {"page": {"selector": "#archivedpatientGrid"}}}
    )


@pytest.mark.asyncio
async def test_ensure_dashboard_citation_panel_clicks_citation_tab():
    from scraper.auth.session_manager import PlaywrightBrowser

    class FakeLocator:
        def __init__(self, page):
            self._page = page
            self.first = self

        async def is_visible(self):
            return self._page.visible

    class FakePage:
        url = "https://www.pakistanlawsite.com/Login/Check"
        visible = False
        activation_calls = 0

        def locator(self, _sel):
            return FakeLocator(self)

        async def click(self, _sel, timeout=0):
            self.activation_calls += 1
            self.visible = True

        async def wait_for_selector(self, _sel, state="", timeout=0):
            if not self.visible:
                raise RuntimeError("still hidden")

    browser = object.__new__(PlaywrightBrowser)
    browser.slot_number = 1
    browser._page = FakePage()

    async def _wrap(coro):
        return await coro

    browser._wrap = _wrap
    await browser.ensure_dashboard_citation_search_panel_visible()
    assert browser._page.activation_calls == 1


@pytest.mark.asyncio
async def test_ensure_dashboard_citation_panel_opens_the_dashboard_from_another_page():
    """From CitationSearch the panel is not on the page: open /Login/Check first, then reveal it."""
    from scraper.auth.session_manager import PlaywrightBrowser

    class FakeLocator:
        def __init__(self, page):
            self._page = page
            self.first = self

        async def is_visible(self):
            return self._page.visible

    class FakePage:
        url = "https://www.pakistanlawsite.com/Login/CitationSearch"
        visible = False
        activation_calls = 0

        def locator(self, _sel):
            return FakeLocator(self)

        async def click(self, _sel, timeout=0):
            self.activation_calls += 1
            self.visible = "/Login/Check" in self.url

        async def wait_for_selector(self, _sel, state="", timeout=0):
            if not self.visible:
                raise RuntimeError("still hidden")

    browser = object.__new__(PlaywrightBrowser)
    browser.slot_number = 1
    browser._page = FakePage()
    visited = []

    async def _goto(url, **_kw):
        visited.append(url)
        browser._page.url = url

    async def _wrap(coro):
        return await coro

    browser.goto = _goto
    browser._wrap = _wrap
    await browser.ensure_dashboard_citation_search_panel_visible()
    assert visited and visited[0].endswith("/Login/Check")
    assert browser._page.activation_calls == 1 and browser._page.visible


def test_panel_detection_reads_the_all_field_list_of_a_map_record():
    """A map record keeps every field under "_all" as a list; that crashed the search harvest with
    AttributeError("'list' object has no attribute 'get'") on 8 October 2026."""
    from scraper.extractors.deterministic import search_map_uses_dashboard_citation_panel
    from scraper.tasks.search_map import build_map_record

    record = build_map_record(
        {"fields": [{"role": "reporter", "name": "cat", "selector": "#Citation_Category_Search_dropdown", "kind": "select"}]},
        "<html></html>",
        "test",
    )
    assert isinstance(record["fields"]["_all"], list)
    assert search_map_uses_dashboard_citation_panel(record) is True
    plain = build_map_record({"fields": [{"role": "keyword", "name": "q", "selector": "#q", "kind": "text"}]}, "<html></html>", "test")
    assert search_map_uses_dashboard_citation_panel(plain) is False
