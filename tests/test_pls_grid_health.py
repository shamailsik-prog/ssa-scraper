from datetime import datetime, timedelta, timezone

import pytest

from scraper.models import ScraperJob
from scraper.pls_grid_health import (
    defer_pls_grid_for_search_harvest,
    grid_rows_remaining,
    pls_job_is_grid_reconcile,
    pls_preempt_running_job_for_search_harvest,
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
        assert defer_pls_grid_for_search_harvest({"pls_stall_watchdog": {"stalled": True, "stalled_reason": "grid_saturated"}}) is False
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


def test_pls_job_is_grid_reconcile_ignores_lock_skip_and_counts_real_grid():
    skip = ScraperJob(
        source_name="PakistanLawSite",
        job_type="scrape",
        pages_scraped=0,
        result_summary={"skipped": "login_session_lock_held", "pages": 0, "staged": 0},
    )
    grid = ScraperJob(
        source_name="PakistanLawSite",
        job_type="scrape",
        pages_scraped=12,
        result_summary={"surface_mode": "citation_grid", "pages_charged": 12},
    )
    assert not pls_job_is_grid_reconcile(skip)
    assert pls_job_is_grid_reconcile(grid)


def test_zero_query_not_failure_when_progress():
    stats = {"surface_mode": "citation_grid", "queries": 0, "citation_grid_windows": 1, "pages_charged": 2}
    cfg = {"citation_grid_cursor": {"row_offset": 100, "last_total_rows": 500}}
    assert pls_zero_query_grid_failure(stats, cfg) is None


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
