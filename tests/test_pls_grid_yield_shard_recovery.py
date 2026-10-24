"""Regression: shard-scoped yield cap must survive unsuccessful citation-grid recovery."""

from datetime import datetime, timedelta, timezone

import pytest

from scraper.auth.session_manager import merge_source_config
from scraper.config import settings
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age_minutes", "window_outcome", "expected_calls", "clears_shard0"),
    [
        (121, {"rows": 1, "wrapped": True, "cursor_unconfirmed": False}, 1, True),
        (1, None, 0, False),
        (121, {"rows": 1, "wrapped": False, "cursor_unconfirmed": True}, 1, False),
        (
            121,
            {"rows": 0, "wrapped": False, "cursor_unconfirmed": False, "citation_grid_stub_stop": True},
            1,
            False,
        ),
    ],
)
async def test_capped_grid_recovery_only_clears_confirmed_shard(
    db, login_source, monkeypatch, age_minutes, window_outcome, expected_calls, clears_shard0
):
    from scraper import pls_grid_health

    now = datetime.now(timezone.utc)
    marker0 = (now - timedelta(minutes=age_minutes)).isoformat()
    marker1 = (now - timedelta(minutes=3)).isoformat()
    await merge_source_config(
        db,
        login_source,
        {
            "grid_yield_since_shard_0": marker0,
            "grid_yield_since_shard_1": marker1,
            "citation_grid_cursor_shard_0": {
                "saturated_at": (now - timedelta(hours=1)).isoformat(),
                "row_offset": 10,
                "last_total_rows": 500,
            },
        },
    )
    await db.commit()
    monkeypatch.setattr(settings, "PLS_GRID_JOURNAL_ROTATION", False)

    async def pending(*args, **kwargs):
        return 5

    monkeypatch.setattr(pls_grid_health, "pls_pending_search_harvest_count", pending)
    pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
    monkeypatch.setattr(
        pipeline, "_citation_grid_limits",
        lambda: {"max_detail": 10, "scan_window": 50, "run_minutes": 0},
    )

    calls = []

    async def fake_window(*args, **kwargs):
        calls.append(True)
        assert window_outcome is not None, "fresh yield marker should skip grid window"
        return dict(window_outcome)

    async def no_persist():
        pass

    monkeypatch.setattr(pipeline, "_run_citation_grid_window", fake_window)
    monkeypatch.setattr(pipeline, "_persist_live_session", no_persist)

    await pipeline.run_citation_grid_surface({})
    assert len(calls) == expected_calls
    cfg = login_source.config_json or {}
    assert cfg.get("grid_yield_since_shard_1") == marker1
    if clears_shard0:
        assert not cfg.get("grid_yield_since_shard_0")
    else:
        assert cfg.get("grid_yield_since_shard_0") == marker0
    await db.rollback()
