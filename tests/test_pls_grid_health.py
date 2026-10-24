from datetime import datetime, timedelta, timezone

import pytest

from scraper.models import ScraperJob
from scraper.models import PlsSearchHarvestQuery
from scraper.pls_grid_health import (
    GRID_YIELD_SINCE_KEY,
    defer_pls_grid_for_search_harvest,
    grid_rows_remaining,
    pls_grid_yield_cap_reached,
    pls_pending_search_harvest_count,
    pls_preempt_running_job_for_search_harvest,
    pls_yield_citation_grid_to_search_harvest,
    pls_zero_query_grid_failure,
)


def test_cursor_resume_never_treated_as_complete_at_offset():
    cfg = {
        "citation_grid_cursor": {"row_offset": 8344, "last_total_rows": 20568},
        "citation_grid_cursor_shard_0": {"row_offset": 4818, "last_total_rows": 20568},
        "citation_grid_cursor_shard_1": {"row_offset": 4271, "last_total_rows": 20568},
    }
    assert grid_rows_remaining(cfg) > 0


def test_zero_query_grid_failure_when_rows_remain():
    stats = {"surface_mode": "citation_grid", "queries": 0, "pages": 0, "citation_grid_windows": 0}
    cfg = {"citation_grid_cursor": {"row_offset": 100, "last_total_rows": 500}}
    msg = pls_zero_query_grid_failure(stats, cfg)
    assert msg and "zero citation-grid progress" in msg


def test_grid_yield_cap_allows_saturated_lap():
    now = datetime.now(timezone.utc)
    cfg = {
        "grid_yield_since_shard_0": (now - timedelta(minutes=121)).isoformat(),
        "citation_grid_cursor": {
            "saturated_at": (now - timedelta(hours=1)).isoformat(),
            "row_offset": 200,
            "last_total_rows": 500,
        },
    }
    assert pls_grid_yield_cap_reached(cfg, 0, now=now) is True
    assert pls_yield_citation_grid_to_search_harvest(cfg, 0, 30, now=now) is None


def test_unsharded_grid_yield_cap_when_either_shard_past_limit():
    now = datetime.now(timezone.utc)
    cfg = {
        "grid_yield_since_shard_1": (now - timedelta(minutes=121)).isoformat(),
        "citation_grid_cursor_shard_0": {
            "saturated_at": (now - timedelta(hours=1)).isoformat(),
            "row_offset": 1,
            "last_total_rows": 500,
        },
        "citation_grid_cursor_shard_1": {
            "saturated_at": (now - timedelta(hours=1)).isoformat(),
            "row_offset": 1,
            "last_total_rows": 500,
        },
    }
    assert pls_grid_yield_cap_reached(cfg, None, now=now) is True
    assert pls_yield_citation_grid_to_search_harvest(cfg, None, 30, now=now) is None


@pytest.mark.asyncio
async def test_pending_count_slot2_only_not_counted_for_slot1(db, monkeypatch):
    from scraper.config import settings

    db.add(
        PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key="search:reporter=PLC|year=2020",
            query_json={"reporter": "PLC", "year": 2020},
            status="pending",
            priority=100,
        )
    )
    await db.flush()
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", False)
    assert await pls_pending_search_harvest_count(db, slot=1) == 0
    assert await pls_pending_search_harvest_count(db, slot=2) == 1
    await db.rollback()


@pytest.mark.asyncio
async def test_stale_in_progress_not_counted_on_stall_path(db):
    from scraper.config import settings

    now = datetime.now(timezone.utc)
    tick = int(settings.PLS_SEARCH_HARVEST_TICK_SECONDS or 1200)
    db.add(
        PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key="search:reporter=PLD|year=2020",
            query_json={"reporter": "PLD", "year": 2020},
            status="in_progress",
            priority=100,
            last_run_at=now - timedelta(seconds=3 * tick),
        )
    )
    await db.flush()
    assert (
        await pls_pending_search_harvest_count(db, slot=1, stall_reason="no_output_while_harvesting", now=now)
        == 0
    )
    await db.rollback()


@pytest.mark.asyncio
async def test_grid_yield_watchdog_reason_recorded(db, login_source):
    from scraper.pls_grid_health import pls_note_search_harvest_grid_yield

    await pls_note_search_harvest_grid_yield(db, login_source, "slot test yield")
    watch = (login_source.config_json or {}).get("pls_stall_watchdog") or {}
    assert watch.get("search_harvest_yield_reason") == "slot test yield"
    await db.rollback()


def test_dispatch_saturated_unsharded_when_both_shard_cursors_saturated():
    from scraper.pls_grid_health import dispatch_saturated

    now = datetime.now(timezone.utc)
    cfg = {
        "citation_grid_cursor_shard_0": {
            "saturated_at": (now - timedelta(hours=1)).isoformat(),
            "row_offset": 100,
            "last_total_rows": 500,
        },
        "citation_grid_cursor_shard_1": {
            "saturated_at": (now - timedelta(hours=1)).isoformat(),
            "row_offset": 50,
            "last_total_rows": 500,
        },
    }
    assert dispatch_saturated(cfg, None, now=now) is True
    assert dispatch_saturated(cfg, 0, now=now) is True
    assert dispatch_saturated({"citation_grid_cursor_shard_0": cfg["citation_grid_cursor_shard_0"]}, None, now=now) is False


def test_yield_saturated_grid_when_search_harvest_pending():
    now = datetime.now(timezone.utc)
    cfg = {
        "citation_grid_cursor": {
            "saturated_at": (now - timedelta(hours=1)).isoformat(),
            "row_offset": 200,
            "last_total_rows": 500,
        }
    }
    reason = pls_yield_citation_grid_to_search_harvest(cfg, None, 30, now=now)
    assert reason and "30 pending" in reason
    assert pls_yield_citation_grid_to_search_harvest(cfg, None, 0, now=now) is None
    assert pls_yield_citation_grid_to_search_harvest(cfg, None, 5, now=now + timedelta(hours=20)) is None


def test_defer_pls_grid_when_stalled_for_search_harvest():
    cfg = {"pls_stall_watchdog": {"stalled": True, "stalled_reason": "no_output_while_harvesting"}}
    from scraper.config import settings
    from scraper.pls_grid_health import pls_search_harvest_may_run

    old = settings.PLS_SEARCH_HARVEST_ENABLED
    try:
        settings.PLS_SEARCH_HARVEST_ENABLED = False
        assert defer_pls_grid_for_search_harvest(cfg) is True
        assert pls_search_harvest_may_run(cfg, pending_gaps=3) is True
        assert pls_search_harvest_may_run(cfg, pending_gaps=0) is False
        assert defer_pls_grid_for_search_harvest({"pls_stall_watchdog": {"stalled": False}}) is False
        assert defer_pls_grid_for_search_harvest({"pls_stall_watchdog": {"stalled": True, "stalled_reason": "grid_saturated"}}) is True
        assert defer_pls_grid_for_search_harvest({}, stall_reason="grid_saturated") is True
        assert pls_search_harvest_may_run({}, pending_gaps=2, stall_reason="grid_saturated") is True
        settings.PLS_SEARCH_HARVEST_ENABLED = True
        assert pls_search_harvest_may_run({}, pending_gaps=0) is True
    finally:
        settings.PLS_SEARCH_HARVEST_ENABLED = old


def test_preempt_zero_output_grid_job_when_stalled_with_gaps():
    now = datetime.now(timezone.utc)
    cfg = {"pls_stall_watchdog": {"stalled": True, "stalled_reason": "no_output_while_harvesting"}}
    old_job = ScraperJob(
        source_name="PakistanLawSite",
        job_type="scrape",
        status="running",
        started_at=now - timedelta(minutes=11),
        pages_scraped=0,
        records_extracted=0,
    )
    young_job = ScraperJob(
        source_name="PakistanLawSite",
        job_type="scrape",
        status="running",
        started_at=now - timedelta(minutes=3),
        pages_scraped=0,
        records_extracted=0,
    )
    assert pls_preempt_running_job_for_search_harvest(old_job, cfg, pending_gaps=5, now=now) is True
    assert pls_preempt_running_job_for_search_harvest(young_job, cfg, pending_gaps=5, now=now) is False
    assert pls_preempt_running_job_for_search_harvest(old_job, cfg, pending_gaps=0, now=now) is False


def test_preempt_citation_grid_job_with_pages_but_zero_staged():
    now = datetime.now(timezone.utc)
    cfg = {"pls_stall_watchdog": {"stalled": True, "stalled_reason": "no_output_while_harvesting"}}
    grid_job = ScraperJob(
        source_name="PakistanLawSite",
        job_type="scrape",
        status="running",
        started_at=now - timedelta(minutes=15),
        pages_scraped=42,
        records_extracted=0,
        result_summary={"surface_mode": "citation_grid", "pages_charged": 42, "staged": 0},
    )
    assert pls_preempt_running_job_for_search_harvest(grid_job, cfg, pending_gaps=5, now=now) is True


def test_zero_query_not_failure_when_progress():
    stats = {"surface_mode": "citation_grid", "queries": 0, "citation_grid_windows": 1, "pages_charged": 2}
    cfg = {"citation_grid_cursor": {"row_offset": 100, "last_total_rows": 500}}
    assert pls_zero_query_grid_failure(stats, cfg) is None


@pytest.mark.asyncio
async def test_citation_grid_midrun_yield_persists_cursor_and_releases_slot_lock(
    db, login_source, monkeypatch
):
    from scraper.auth.session_manager import merge_source_config
    from scraper.config import settings
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(settings, "PLS_GRID_JOURNAL_ROTATION", False)
    await merge_source_config(
        db,
        login_source,
        {
            "citation_grid_cursor_shard_0": {
                "saturated_at": (now - timedelta(hours=1)).isoformat(),
                "row_offset": 10,
                "last_total_rows": 500,
            }
        },
    )
    await db.commit()

    pipeline = PakistanLawSitePipeline(db, login_source, reporter_shard=0)
    released: list[bool] = []

    class FakeSlotLock:
        async def release(self):
            released.append(True)

    pipeline._slot_lock = FakeSlotLock()
    window_calls = 0
    yield_after_window = False

    async def fake_window(self, search_map, limits):
        nonlocal window_calls
        window_calls += 1
        key = "citation_grid_cursor_shard_0"
        cur = dict((self.source.config_json or {}).get(key) or {})
        cur["row_offset"] = 77
        await merge_source_config(self.db, self.source, {key: cur})
        await self.db.commit()
        return {"rows": 1, "wrapped": False, "cursor_unconfirmed": False}

    async def fake_maybe(self):
        nonlocal yield_after_window
        if yield_after_window:
            self.stats["yielded_for_search_harvest"] = "search-harvest pending"
            return True
        yield_after_window = True
        return False

    async def noop_persist(self):
        return None

    monkeypatch.setattr(
        pipeline,
        "_citation_grid_limits",
        lambda: {"max_detail": 10, "scan_window": 50, "run_minutes": 60},
    )
    monkeypatch.setattr(PakistanLawSitePipeline, "_run_citation_grid_window", fake_window)
    monkeypatch.setattr(
        PakistanLawSitePipeline, "_maybe_yield_citation_grid_for_search_harvest", fake_maybe
    )
    monkeypatch.setattr(PakistanLawSitePipeline, "_persist_live_session", noop_persist)

    await pipeline.run_citation_grid_surface({})
    assert window_calls == 1
    assert pipeline.stats.get("yielded_for_search_harvest")
    cfg = login_source.config_json or {}
    assert cfg.get("citation_grid_cursor_shard_0", {}).get("row_offset") == 77

    if pipeline.stats.get("yielded_for_search_harvest") and pipeline._slot_lock is not None:
        await pipeline._slot_lock.release()
        pipeline._slot_lock = None

    assert released == [True]
    assert pipeline._slot_lock is None
    await db.rollback()


@pytest.mark.asyncio
async def test_heartbeat_job_raises_when_dispatcher_preempted(db, login_source):
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline, ScraperJobPreempted

    job = ScraperJob(
        source_id=login_source.id,
        source_name="PakistanLawSite",
        job_type="scrape",
        status="failed",
        error_message="preempted for search-harvest gap recovery while promotion stalled (zero-output grid job)",
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    await db.flush()
    pipeline = PakistanLawSitePipeline(db, login_source, job_id=job.id)
    pipeline.stats = {"pages_charged": 0, "staged": 0}
    with pytest.raises(ScraperJobPreempted, match="preempted"):
        await pipeline._heartbeat_job()
