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
        source_state="ACTIVE",
        now=NOW,
    )
    assert verdict["stalled"] is True
    assert verdict["threshold_hours"] == 0.5


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
    assert is_playwright_harvest_browser_fatal(BrowserDisconnected("Target closed"))
    assert is_playwright_harvest_browser_fatal(RuntimeError("Page crashed"))
    assert not is_playwright_harvest_browser_fatal(RuntimeError("selector not found"))


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
        BrowserDisconnected("Target closed"),
    )
    await db.refresh(row)
    assert out["browser_fatal"] is True
    assert row.status == "pending"
    assert row.rows_seen == 12
    assert row.rows_new == 3
    assert row.pages_enumerated == 2
    assert row.cursor_json == {"page": 2, "row_index": 4}
    assert "Target closed" in (row.last_error or "")
