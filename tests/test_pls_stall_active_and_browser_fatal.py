"""PLS stall threshold when ACTIVE and search-harvest Playwright fatals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scraper.auth.session_manager import BrowserDisconnected, is_playwright_harvest_browser_fatal
from scraper.config import settings
from scraper.pls_grid_health import pls_stall_verdict_for_config, stall_threshold_hours
from scraper.tasks import pls_search_harvest as harvest

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def test_stall_threshold_active_uses_minutes_default():
    assert stall_threshold_hours(source_state="ACTIVE", harvest_paused=False) == 0.5


def test_stall_threshold_active_respects_settings(monkeypatch):
    monkeypatch.setattr(settings, "PLS_STALL_NO_PROMOTION_MINUTES_WHEN_ACTIVE", 45.0)
    assert stall_threshold_hours(source_state="ACTIVE", harvest_paused=False) == 0.75


def test_stall_verdict_active_flags_35_min_idle(monkeypatch):
    monkeypatch.setattr(settings, "PLS_STALL_NO_PROMOTION_MINUTES_WHEN_ACTIVE", 30.0)
    verdict = pls_stall_verdict_for_config(
        {},
        last_promotion_at=NOW - timedelta(minutes=35),
        last_staging_at=NOW - timedelta(minutes=35),
        source_state="ACTIVE",
        now=NOW,
    )
    assert verdict["stalled"] is True
    assert verdict["threshold_hours"] == 0.5


def test_stall_verdict_active_not_stalled_when_staging_still_moving(monkeypatch):
    monkeypatch.setattr(settings, "PLS_STALL_NO_PROMOTION_MINUTES_WHEN_ACTIVE", 30.0)
    verdict = pls_stall_verdict_for_config(
        {},
        last_promotion_at=NOW - timedelta(minutes=35),
        last_staging_at=NOW - timedelta(minutes=5),
        source_state="ACTIVE",
        now=NOW,
    )
    assert verdict["stalled"] is False


def test_stall_verdict_active_not_stalled_at_20_min(monkeypatch):
    monkeypatch.setattr(settings, "PLS_STALL_NO_PROMOTION_MINUTES_WHEN_ACTIVE", 30.0)
    verdict = pls_stall_verdict_for_config(
        {},
        last_promotion_at=NOW - timedelta(minutes=20),
        source_state="ACTIVE",
        now=NOW,
    )
    assert verdict["stalled"] is False


def test_playwright_harvest_fatal_markers():
    assert not is_playwright_harvest_browser_fatal(BrowserDisconnected("Target closed"))
    assert not is_playwright_harvest_browser_fatal(BrowserDisconnected("Browser has been closed"))
    assert is_playwright_harvest_browser_fatal(RuntimeError("Page crashed"))
    assert is_playwright_harvest_browser_fatal(BrowserDisconnected("Page crashed"))
    assert not is_playwright_harvest_browser_fatal(RuntimeError("selector not found"))


@pytest.mark.asyncio
async def test_real_playwright_error_wrapper_preserves_crash_classification():
    from playwright.async_api import Error as PWError
    from scraper.auth.session_manager import PlaywrightBrowser

    async def page_crash():
        raise PWError("Page crashed")

    with pytest.raises(BrowserDisconnected) as caught:
        await PlaywrightBrowser._wrap(None, page_crash())
    assert is_playwright_harvest_browser_fatal(caught.value)


@pytest.mark.asyncio
async def test_pagination_crash_is_not_swallowed(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from scraper.extractors.hybrid_extractor import HybridExtractor

    row = SimpleNamespace(
        query_key="search:reporter=PLD|year=2026",
        query_json={"reporter": "PLD", "year": 2026},
        cursor_json={"page": 1, "row_index": 0},
        pages_enumerated=0,
        rows_seen=0,
        rows_known=0,
        rows_new=0,
        site_total_results=None,
        gap_size=0,
        status="in_progress",
        last_error=None,
    )
    runner = object.__new__(harvest.SearchHarvestRunner)
    runner.db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock())
    runner.source = SimpleNamespace()
    runner.pipeline = SimpleNamespace(
        fetch_results=AsyncMock(return_value=SimpleNamespace(html="<html></html>", url="https://example.org/page1")),
        fetch_detail=AsyncMock(side_effect=BrowserDisconnected("Page crashed")),
        runner=SimpleNamespace(browser=None),
        local_engine=None,
    )
    runner.memory = SimpleNamespace(note_window=lambda: None)
    runner.dry_run = True
    runner._maybe_recycle_browser = AsyncMock()
    runner._maybe_spot_check = AsyncMock()
    runner._pace_sleep = AsyncMock()
    monkeypatch.setattr(harvest, "unmapped_harvest_reason", lambda *args: None)
    monkeypatch.setattr(harvest, "build_harvest_form_values", lambda *args: {})
    monkeypatch.setattr(harvest, "load_known_citation_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(harvest, "release_page_result", lambda page: None)
    monkeypatch.setattr(
        HybridExtractor,
        "extract_result_rows",
        AsyncMock(return_value=SimpleNamespace(data={"result_rows": [], "next_page": "https://example.org/page2"})),
    )
    with pytest.raises(BrowserDisconnected, match="Page crashed"):
        await runner.run_query(row, {"fields": {}}, max_pages=10)
    assert row.cursor_json["next_url"] == "https://example.org/page2"



@pytest.mark.asyncio
async def test_release_harvest_query_keeps_counters(db):
    from scraper.models import PlsSearchHarvestQuery

    row = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2020",
        query_json={"reporter": "PLD", "year": 2020},
        status="in_progress",
        rows_seen=12,
        rows_new=3,
        pages_enumerated=2,
        cursor_json={"page": 2, "row_index": 4},
    )
    db.add(row)
    await db.flush()
    out = await harvest._release_harvest_query_after_browser_fatal(
        db,
        row,
        1,
        RuntimeError("Page crashed"),
    )
    await db.refresh(row)
    assert out["browser_fatal"] is True
    assert row.status == "pending"
    assert row.rows_seen == 12
    assert row.rows_new == 3
    assert row.pages_enumerated == 2
    assert row.cursor_json == {"page": 2, "row_index": 4}
    assert "Page crashed" in (row.last_error or "")
