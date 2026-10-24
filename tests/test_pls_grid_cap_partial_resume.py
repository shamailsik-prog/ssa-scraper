"""A capped grid lap must resume after a verified partial window, not wait 120 more minutes.

All grid windows are mocked. No licensed source is contacted.
"""
from datetime import datetime, timedelta, timezone

import pytest

from scraper.auth.session_manager import merge_source_config
from scraper.config import settings
from scraper.pls_grid_health import (
    pls_consume_grid_yield_cap_bypass,
    pls_grid_yield_cap_reached,
)
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("processed", "wrapped", "unconfirmed", "expected_marker"),
    [
        (1, False, False, "original"),  # 1/500 processed: next job must resume
        (50, False, False, "original"),  # detail-cap partial progress
        (0, False, False, "fresh"),      # no progress: retain retry backoff
        (1, False, True, "fresh"),       # unconfirmed cursor: no verified progress
        (1, True, False, "cleared"),     # completed lap: search may regain priority
    ],
)
async def test_consumed_cap_partial_grid_lap_resumes_without_new_wait(
    db, login_source, monkeypatch, processed, wrapped, unconfirmed, expected_marker
):
    now = datetime.now(timezone.utc)
    marker0 = (now - timedelta(minutes=121)).isoformat()
    marker1 = (now - timedelta(minutes=3)).isoformat()
    await merge_source_config(
        db,
        login_source,
        {
            "grid_yield_since_shard_0": marker0,
            "grid_yield_since_shard_1": marker1,
            "citation_grid_cursor_shard_0": {
                "row_offset": 10,
                "last_total_rows": 500,
                "saturated_at": (now - timedelta(hours=1)).isoformat(),
            },
        },
    )
    await db.commit()
    monkeypatch.setattr(settings, "PLS_GRID_JOURNAL_ROTATION", False)
    assert pls_grid_yield_cap_reached(dict(login_source.config_json or {}), 0, now=now)
    assert await pls_consume_grid_yield_cap_bypass(
        db, login_source, reporter_shard=0, now=now
    )
    pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
    pipeline.stats["grid_yield_cap_bypass_consumed"] = True
    pipeline.stats["grid_cap_prior_yield_markers"] = {
        "grid_yield_since_shard_0": marker0
    }
    monkeypatch.setattr(
        pipeline,
        "_citation_grid_limits",
        lambda: {"max_detail": 10, "scan_window": 50, "run_minutes": 0},
    )

    async def fake_window(*args, **kwargs):
        return {
            "rows": 1,
            "processed": processed,
            "wrapped": wrapped,
            "reached_end": wrapped,
            "cursor_unconfirmed": unconfirmed,
        }

    async def no_persist():
        return None

    monkeypatch.setattr(pipeline, "_run_citation_grid_window", fake_window)
    monkeypatch.setattr(pipeline, "_persist_live_session", no_persist)
    await pipeline.run_citation_grid_surface({})

    cfg = dict(login_source.config_json or {})
    assert cfg.get("grid_yield_since_shard_1") == marker1
    if expected_marker == "original":
        assert cfg.get("grid_yield_since_shard_0") == marker0
        assert pls_grid_yield_cap_reached(cfg, 0, now=now)
    elif expected_marker == "fresh":
        assert cfg.get("grid_yield_since_shard_0") == now.isoformat()
        assert not pls_grid_yield_cap_reached(cfg, 0, now=now)
    else:
        assert not cfg.get("grid_yield_since_shard_0")
    await db.rollback()
