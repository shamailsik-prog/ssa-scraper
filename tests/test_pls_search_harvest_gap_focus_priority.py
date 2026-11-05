"""search_harvest_priority must follow GAP_FOCUS claim eligibility.

A pending gap-0 backlog blocked by the open-gap throttle is not work a tick can
claim. The login-session yield gate was still counting those rows and skipping
citation-grid as search_harvest_priority.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scraper.config import settings
from scraper.models import PlsSearchHarvestQuery, ScraperJob, ScraperSource
from scraper.pls_grid_health import (
    pls_pending_search_harvest_count,
    pls_preempt_running_job_for_search_harvest,
    pls_yield_citation_grid_to_search_harvest,
)

STALL = "no_output_while_harvesting"


def _flags(monkeypatch, *, gap_focus: bool) -> None:
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_GAP_FOCUS", gap_focus)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", False)


def _row(**kwargs) -> PlsSearchHarvestQuery:
    body = dict(
        source_name="PakistanLawSite",
        status="pending",
        priority=100,
        gap_size=0,
        query_json={"reporter": "PLD", "year": 2020},
    )
    body.update(kwargs)
    return PlsSearchHarvestQuery(**body)


def _blocked_gap0(now: datetime) -> list[PlsSearchHarvestQuery]:
    """One open gap-0 inside the 6h window, plus a cooled gap-0 the throttle blocks."""
    return [
        _row(
            query_key="search:reporter=YLR|year=2026",
            query_json={"reporter": "YLR", "year": 2026},
            status="pending",
            gap_size=0,
            last_run_at=now - timedelta(hours=1),
        ),
        _row(
            query_key="search:reporter=PLD|year=2024",
            query_json={"reporter": "PLD", "year": 2024},
            status="pending",
            gap_size=0,
            last_run_at=now - timedelta(hours=8),
        ),
    ]


def _watchdog_cfg() -> dict:
    return {"pls_stall_watchdog": {"stalled": True, "stalled_reason": STALL}}


def _yield_reason(pending: int, *, shard: int = 0):
    return pls_yield_citation_grid_to_search_harvest(
        _watchdog_cfg(),
        shard,
        pending,
        stall_reason=STALL,
    )


@pytest.mark.asyncio
async def test_gap_focus_off_still_counts_gap0_blocked_by_the_open_throttle(db, monkeypatch):
    _flags(monkeypatch, gap_focus=False)
    now = datetime.now(timezone.utc)
    db.add_all(_blocked_gap0(now))
    await db.flush()

    pending = await pls_pending_search_harvest_count(db, reporter_shard=0, now=now)
    assert pending == 2
    assert _yield_reason(pending) is not None
    await db.rollback()


@pytest.mark.asyncio
async def test_gap_focus_on_does_not_count_gap0_blocked_by_the_open_throttle(db, monkeypatch):
    _flags(monkeypatch, gap_focus=True)
    now = datetime.now(timezone.utc)
    db.add_all(_blocked_gap0(now))
    await db.flush()

    pending = await pls_pending_search_harvest_count(db, reporter_shard=0, now=now)
    unscoped = await pls_pending_search_harvest_count(db, now=now)
    assert pending == 0
    assert unscoped == 0
    assert _yield_reason(pending) is None
    job = ScraperJob(
        source_name="PakistanLawSite",
        job_type="scrape",
        status="running",
        started_at=now - timedelta(minutes=20),
        pages_scraped=0,
        records_extracted=0,
    )
    assert (
        pls_preempt_running_job_for_search_harvest(
            job, _watchdog_cfg(), pending_gaps=unscoped, stall_reason=STALL, now=now
        )
        is False
    )
    await db.rollback()


@pytest.mark.asyncio
async def test_gap_focus_on_still_counts_a_claimable_positive_gap(db, monkeypatch):
    _flags(monkeypatch, gap_focus=True)
    now = datetime.now(timezone.utc)
    db.add_all(_blocked_gap0(now))
    db.add(
        _row(
            query_key="search:reporter=PLD|year=2017",
            query_json={"reporter": "PLD", "year": 2017},
            status="pending",
            gap_size=12,
            last_run_at=now - timedelta(hours=8),
        )
    )
    db.add(
        _row(
            query_key="search:reporter=PLC|year=2017",
            query_json={"reporter": "PLC", "year": 2017},
            status="pending",
            gap_size=40,
        )
    )
    await db.flush()

    pending = await pls_pending_search_harvest_count(db, reporter_shard=0, now=now)
    assert pending == 1
    assert _yield_reason(pending) is not None
    assert await pls_pending_search_harvest_count(db, reporter_shard=1, now=now) == 1
    await db.rollback()


@pytest.mark.asyncio
async def test_gap_focus_on_counts_a_cooled_gap0_when_nothing_blocks_it(db, monkeypatch):
    _flags(monkeypatch, gap_focus=True)
    now = datetime.now(timezone.utc)
    db.add(
        _row(
            query_key="search:reporter=PLD|year=2024",
            query_json={"reporter": "PLD", "year": 2024},
            status="pending",
            gap_size=0,
            last_run_at=now - timedelta(hours=8),
        )
    )
    await db.flush()

    pending = await pls_pending_search_harvest_count(db, slot=1, now=now)
    assert pending == 1
    assert _yield_reason(pending) is not None
    await db.rollback()


@pytest.mark.asyncio
async def test_gap_focus_stall_count_ignores_a_fresh_unclaimable_gap0(db, monkeypatch):
    now = datetime.now(timezone.utc)
    db.add(
        _row(
            query_key="search:reporter=YLR|year=2026",
            query_json={"reporter": "YLR", "year": 2026},
            status="in_progress",
            gap_size=0,
            last_run_at=now,
        )
    )
    db.add(
        _row(
            query_key="search:reporter=CLC|year=2026",
            query_json={"reporter": "CLC", "year": 2026},
            status="in_progress",
            gap_size=9,
            last_run_at=now,
        )
    )
    await db.flush()

    _flags(monkeypatch, gap_focus=False)
    assert (
        await pls_pending_search_harvest_count(
            db, slot=1, stall_reason=STALL, now=now
        )
        == 2
    )

    _flags(monkeypatch, gap_focus=True)
    assert (
        await pls_pending_search_harvest_count(
            db, slot=1, stall_reason=STALL, now=now
        )
        == 1
    )
    await db.rollback()


async def _pls_source(db):
    from sqlalchemy import select

    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))
    ).scalars().first()
    source.state = "ACTIVE"
    source.is_active = True
    source.config_json = {**dict(source.config_json or {}), **_watchdog_cfg()}
    return source


@pytest.mark.asyncio
async def test_run_source_gap_focus_off_still_defers_blocked_gap0(db, monkeypatch):
    from scraper.harvest_mode import set_harvest_mode
    from scraper.tasks import dispatcher
    from scraper.tasks.dispatcher import run_source
    from tests.test_dispatcher import _activate_pls_slots

    _flags(monkeypatch, gap_focus=False)
    now = datetime.now(timezone.utc)
    source = await _pls_source(db)
    await _activate_pls_slots(db, (1,))
    db.add_all(_blocked_gap0(now))
    await set_harvest_mode(db, "updates", changed_by="qa", reason="gap focus off")
    await db.commit()

    called = {"n": 0}

    async def fake_connector_for(_source):
        async def fake_connector(_source_obj, _db, **_kwargs):
            called["n"] += 1
            return {"pages": 1, "staged": 1}

        return fake_connector

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    result = await run_source(source.source_name, reporter_shard=0)
    assert result["skipped"] == "search_harvest_priority"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_run_source_gap_focus_on_does_not_defer_blocked_gap0(db, monkeypatch):
    from scraper.harvest_mode import set_harvest_mode
    from scraper.tasks import dispatcher
    from scraper.tasks.dispatcher import run_source
    from tests.test_dispatcher import _activate_pls_slots

    _flags(monkeypatch, gap_focus=True)
    now = datetime.now(timezone.utc)
    source = await _pls_source(db)
    await _activate_pls_slots(db, (1,))
    db.add_all(_blocked_gap0(now))
    await set_harvest_mode(db, "updates", changed_by="qa", reason="gap focus on")
    await db.commit()

    called = {"n": 0}

    async def fake_connector_for(_source):
        async def fake_connector(_source_obj, _db, **_kwargs):
            called["n"] += 1
            return {"pages": 1, "staged": 1}

        return fake_connector

    monkeypatch.setattr(dispatcher, "_connector_for", fake_connector_for)
    result = await run_source(source.source_name, reporter_shard=0)
    assert result.get("skipped") != "search_harvest_priority"
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_dispatch_gap_focus_on_does_not_skip_for_fresh_unclaimable_gap0(db, monkeypatch):
    from scraper.harvest_mode import set_harvest_mode
    from scraper.tasks.celery_app import app
    from scraper.tasks.dispatcher import dispatch_due_sources
    from tests.test_dispatcher import _prepare_pls_due

    _flags(monkeypatch, gap_focus=True)
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_ENABLED", False)
    monkeypatch.setattr(settings, "PLS_GRID_JOURNAL_ROTATION", False)
    monkeypatch.setattr(settings, "BACKFILL_LOGIN_SESSION_CONCURRENCY", 2)
    now = datetime.now(timezone.utc)
    await _prepare_pls_due(db, (1, 2))
    db.add(
        _row(
            query_key="search:reporter=YLR|year=2026",
            query_json={"reporter": "YLR", "year": 2026},
            status="in_progress",
            gap_size=0,
            last_run_at=now,
        )
    )
    db.add(
        _row(
            query_key="search:reporter=PLC|year=2026",
            query_json={"reporter": "PLC", "year": 2026},
            status="in_progress",
            gap_size=0,
            last_run_at=now,
        )
    )
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="gap focus dispatch")
    await db.commit()

    sent: list[tuple] = []
    monkeypatch.setattr(
        app,
        "send_task",
        lambda name, args=(), kwargs=None, queue=None, **_: sent.append((name, kwargs or {}, queue)),
    )
    result = await dispatch_due_sources()
    assert "PakistanLawSite:search_harvest_priority" not in result["skipped_saturated"]
    assert "PakistanLawSite:shard0" in result["queued"]
    assert "PakistanLawSite:shard1" in result["queued"]
    assert any(name == "scraper.tasks.dispatcher.run_login_session_job" for name, _kwargs, _queue in sent)
