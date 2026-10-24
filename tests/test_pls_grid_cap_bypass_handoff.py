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
    pipeline._cap_lap = True

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
    pipeline._cap_lap = True
    await pipeline.run_citation_grid_surface({})
    assert windows == ["PLD"]
    assert not pipeline.stats.get("yielded_for_search_harvest")
    await db.rollback()


@pytest.mark.asyncio
async def test_cap_bypass_runs_one_window_then_yields_and_restarts_timer(db, login_source, monkeypatch):
    """3h-old yield timer + saturated grid + pending rows: one recovery window, then yield."""
    from scraper.auth.session_manager import merge_source_config
    from scraper import pls_grid_health

    now = datetime.now(timezone.utc)
    lap = now.isoformat()
    await merge_source_config(
        db,
        login_source,
        {
            "grid_cap_lap_at": lap,
            "grid_yield_since_shard_0": lap,
            journal_cursor_key("PLD"): _recent_cursor(),
            "citation_grid_cursor_shard_0": {
                "saturated_at": (now - timedelta(hours=1)).isoformat(),
                "row_offset": 10,
                "last_total_rows": 500,
            },
        },
    )
    await db.commit()

    pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
    pipeline.stats["grid_yield_cap_bypass_consumed"] = True
    pipeline._cap_lap = True
    monkeypatch.setattr(pipeline, "_journal_candidates", lambda: ["PLD"])
    monkeypatch.setattr(
        pipeline,
        "_citation_grid_limits",
        lambda: {"max_detail": 10, "scan_window": 50, "run_minutes": 5},
    )

    async def pending(*args, **kwargs):
        return 30

    monkeypatch.setattr(pls_grid_health, "pls_pending_search_harvest_count", pending)

    window_calls = []
    yield_notes = []

    async def fake_window(search_map, limits):
        window_calls.append(1)
        return {
            "rows": 2,
            "processed": 1,
            "wrapped": False,
            "reached_end": False,
            "cursor_unconfirmed": False,
        }

    real_note_yield = pls_grid_health.pls_note_search_harvest_grid_yield

    async def spy_note_yield(db, source, reason, **kwargs):
        yield_notes.append(reason)
        await real_note_yield(db, source, reason, **kwargs)

    async def noop_persist():
        return None

    monkeypatch.setattr(pipeline, "_run_citation_grid_window", fake_window)
    monkeypatch.setattr(pipeline, "_persist_live_session", noop_persist)
    monkeypatch.setattr(pls_grid_health, "pls_note_search_harvest_grid_yield", spy_note_yield)

    await pipeline.run_citation_grid_surface({})
    assert len(window_calls) >= 1
    assert pipeline.stats.get("yielded_for_search_harvest")
    assert yield_notes
    cfg = login_source.config_json or {}
    assert cfg.get("grid_cap_lap_window_at")
    watch = cfg.get("pls_stall_watchdog") or {}
    assert watch.get("search_harvest_yield_reason")
    assert watch.get("search_harvest_yield_at")
    await db.rollback()
