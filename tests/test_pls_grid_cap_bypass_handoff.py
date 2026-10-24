"""Regression checks for consumed citation-grid recovery cap.

All grid windows are mocked: no licensed site is contacted.
"""
from datetime import datetime, timedelta, timezone

import pytest

from scraper.pls_grid_health import journal_cursor_key
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline


def _recent_cursor(*, unsupported=False):
    now = datetime.now(timezone.utc)
    cursor = {"saturated_at": (now - timedelta(hours=1)).isoformat()}
    if unsupported:
        cursor["unsupported_at"] = now.isoformat()
    return cursor


def test_consumed_grid_cap_reopens_saturated_but_not_unsupported_journals(db, login_source, monkeypatch):
    pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
    monkeypatch.setattr(pipeline, "_journal_candidates", lambda: ["PLD", "SCMR"])
    login_source.config_json = {
        **dict(login_source.config_json or {}),
        journal_cursor_key("PLD"): _recent_cursor(),
        journal_cursor_key("SCMR"): _recent_cursor(unsupported=True),
    }
    # Ordinary dispatch respects saturation and unsupported markers.
    assert pipeline._pick_grid_journal() is None
    pipeline.stats["grid_yield_cap_bypass_consumed"] = True
    assert pipeline._pick_grid_journal() == "PLD"
    # A licensed-site mismatch is not a reason to bypass the unsupported guard.
    login_source.config_json = {
        **dict(login_source.config_json or {}),
        journal_cursor_key("PLD"): _recent_cursor(unsupported=True),
    }
    assert pipeline._pick_grid_journal() is None


@pytest.mark.asyncio
async def test_consumed_grid_cap_does_not_immediately_yield_again(db, login_source, monkeypatch):
    from scraper import pls_grid_health

    pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
    pipeline.stats["grid_yield_cap_bypass_consumed"] = True

    async def unexpected_pending_query(*args, **kwargs):
        raise AssertionError("cap-bypass job must not re-yield before grid recovery")

    monkeypatch.setattr(pls_grid_health, "pls_pending_search_harvest_count", unexpected_pending_query)
    assert await pipeline._maybe_yield_citation_grid_for_search_harvest() is False


@pytest.mark.asyncio
async def test_consumed_grid_cap_reaches_saturated_journal_window(db, login_source, monkeypatch):
    pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
    pipeline.stats["grid_yield_cap_bypass_consumed"] = True
    monkeypatch.setattr(pipeline, "_journal_candidates", lambda: ["PLD"])
    login_source.config_json = {
        **dict(login_source.config_json or {}),
        journal_cursor_key("PLD"): _recent_cursor(),
    }
    monkeypatch.setattr(
        pipeline,
        "_citation_grid_limits",
        lambda: {"max_detail": 10, "scan_window": 50, "run_minutes": 0},
    )
    windows = []

    async def fake_window(search_map, limits):
        windows.append(pipeline.grid_journal)
        return {"rows": 1, "processed": 1, "wrapped": True, "reached_end": True, "cursor_unconfirmed": False}

    async def noop_persist():
        return None

    monkeypatch.setattr(pipeline, "_run_citation_grid_window", fake_window)
    monkeypatch.setattr(pipeline, "_persist_live_session", noop_persist)
    await pipeline.run_citation_grid_surface({})
    assert windows == ["PLD"]
    assert not pipeline.stats.get("yielded_for_search_harvest")
    await db.rollback()
