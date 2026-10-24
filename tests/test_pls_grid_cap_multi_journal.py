"""Multi-journal grid recovery must not clear the yield cap after one journal.

Grid windows are mocked; no licensed source is contacted.
"""
from datetime import datetime, timedelta, timezone

import pytest

from scraper.auth.session_manager import merge_source_config
from scraper.pls_grid_health import pls_consume_grid_yield_cap_bypass
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline


@pytest.mark.asyncio
async def test_cap_recovery_waits_for_every_supported_journal_across_jobs(
    db, login_source, monkeypatch
):
    marker = (datetime.now(timezone.utc) - timedelta(minutes=121)).isoformat()
    other_marker = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    await merge_source_config(db, login_source, {
        "grid_yield_since_shard_0": marker,
        "grid_yield_since_shard_1": other_marker,
    })
    await db.commit()

    async def no_persist():
        return None

    # A job can have a one-window budget. Each completed journal must be
    # credited durably without crediting the other journal's unvisited rows.
    for journal, completed, expected_done in [
        ("PLD", True, ["PLD"]),
        ("SCMR", False, ["PLD"]),
        ("SCMR", True, None),
    ]:
        assert await pls_consume_grid_yield_cap_bypass(
            db, login_source, reporter_shard=0
        )
        pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
        pipeline.stats["grid_yield_cap_bypass_consumed"] = True
        pipeline.stats["grid_cap_prior_yield_markers"] = {
            "grid_yield_since_shard_0": marker
        }
        pipeline._cap_lap = True
        monkeypatch.setattr(pipeline, "_journal_candidates", lambda: ["PLD", "SCMR"])
        monkeypatch.setattr(pipeline, "_citation_grid_limits", lambda: {
            "max_detail": 10, "scan_window": 50, "run_minutes": 0,
        })
        monkeypatch.setattr(pipeline, "_persist_live_session", no_persist)
        visited = []

        async def fake_window(*args, **kwargs):
            visited.append(pipeline.grid_journal)
            return {
                "rows": 1,
                "processed": 1,
                "wrapped": completed,
                "reached_end": completed,
                "cursor_unconfirmed": False,
            }

        monkeypatch.setattr(pipeline, "_run_citation_grid_window", fake_window)
        await pipeline.run_citation_grid_surface({})
        assert visited == [journal]
        cfg = dict(login_source.config_json or {})
        assert cfg.get("grid_yield_since_shard_1") == other_marker
        progress = cfg.get("grid_cap_journals_completed_shard_0")
        if expected_done is None:
            assert cfg.get("grid_yield_since_shard_0") is None
            assert progress is None
        else:
            assert cfg.get("grid_yield_since_shard_0") == marker
            assert progress and progress["done"] == expected_done
    await db.rollback()


@pytest.mark.asyncio
async def test_unconfirmed_journal_cannot_claim_full_recovery(db, login_source, monkeypatch):
    marker = (datetime.now(timezone.utc) - timedelta(minutes=121)).isoformat()
    await merge_source_config(db, login_source, {"grid_yield_since_shard_0": marker})
    await db.commit()
    assert await pls_consume_grid_yield_cap_bypass(db, login_source, reporter_shard=0)
    pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
    pipeline.stats["grid_yield_cap_bypass_consumed"] = True
    pipeline.stats["grid_cap_prior_yield_markers"] = {
        "grid_yield_since_shard_0": marker
    }
    pipeline._cap_lap = True
    monkeypatch.setattr(pipeline, "_journal_candidates", lambda: ["PLD", "SCMR"])
    monkeypatch.setattr(pipeline, "_citation_grid_limits", lambda: {
        "max_detail": 10, "scan_window": 50, "run_minutes": 0,
    })

    async def fake_window(*args, **kwargs):
        return {
            "rows": 1, "processed": 1, "wrapped": True,
            "reached_end": True, "cursor_unconfirmed": True,
        }

    async def no_persist():
        return None

    monkeypatch.setattr(pipeline, "_run_citation_grid_window", fake_window)
    monkeypatch.setattr(pipeline, "_persist_live_session", no_persist)
    await pipeline.run_citation_grid_surface({})
    cfg = dict(login_source.config_json or {})
    assert cfg.get("grid_cap_journals_completed_shard_0") is None
    assert cfg.get("grid_yield_since_shard_0") is not None
    await db.rollback()
