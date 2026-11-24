"""Largest remaining gaps are claimed before empty citations, and zero-yield searches cool off.

Production after #431 (MAIN, 24 Nov 2026): citation searches were claimed and every one
returned rows_new=0 staged=0, while SCMR|2025 (remaining gap 263) waited behind that backlog.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scraper.config import settings
from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import fresh_cursor_keeping_annexure_state
from scraper.tasks import pls_search_harvest as harvest

SLOT1 = "PLD,SCMR,CLC,YLR,CLD,GBLR"


def _enable(monkeypatch, *, gap_focus: bool = True, tick_seconds: int = 30) -> None:
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_GAP_FOCUS", gap_focus)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", SLOT1)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", tick_seconds)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", False)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_LEASE_FACTOR", 2)


def _citation_slot(query_key: str) -> int:
    return 1 + (hashlib.md5(query_key.encode("utf-8")).digest()[0] % 2)


def _slot1_citation_key() -> str:
    n = 0
    while True:
        key = f"search:citation=1979 SCMR {n}"
        if _citation_slot(key) == 1:
            return key
        n += 1


def _citation(key: str, *, when: datetime) -> PlsSearchHarvestQuery:
    return PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key=key,
        query_json={"citation": key.removeprefix("search:citation=")},
        status="pending",
        priority=1,
        gap_size=0,
        rows_new=0,
        slot=None,
        last_run_at=when,
    )


def _year(**kwargs) -> PlsSearchHarvestQuery:
    body = dict(
        source_name="PakistanLawSite",
        query_key="search:reporter=SCMR|year=2025",
        query_json={"reporter": "SCMR", "year": 2025},
        status="pending",
        priority=900,
        gap_size=263,
        slot=None,
    )
    body.update(kwargs)
    return PlsSearchHarvestQuery(**body)


async def _claim(db, slot: int = 1):
    row, lock = await harvest.claim_next_query(db, slot, redis_client=None)
    try:
        return row
    finally:
        if lock is not None:
            await lock.release()


@pytest.mark.asyncio
async def test_size_263_pending_gap_is_claimed_before_a_harvested_citation(db, monkeypatch):
    """Both rows are due. The size-263 year gap wins over a size-0 citation."""
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    db.add(_citation(_slot1_citation_key(), when=now - timedelta(days=1)))
    db.add(_year(last_run_at=None))
    await db.commit()

    row = await _claim(db)
    assert row is not None
    assert row.query_key == "search:reporter=SCMR|year=2025"
    assert row.gap_size == 263


@pytest.mark.asyncio
async def test_pending_year_with_remaining_gap_263_beats_a_citation_inside_the_gap0_cooldown(db, monkeypatch):
    """gap_size was left at 0, but site_total - rows_known is still 263.

    A last run inside the 6h gap-0 window must not let a size-0 citation cut ahead.
    """
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    db.add(_citation(_slot1_citation_key(), when=now - timedelta(hours=2)))
    db.add(
        _year(
            gap_size=0,
            site_total_results=263,
            rows_known=0,
            rows_seen=0,
            rows_new=0,
            last_run_at=now - timedelta(hours=1),
        )
    )
    await db.commit()

    row = await _claim(db)
    assert row is not None
    assert row.query_key == "search:reporter=SCMR|year=2025"


@pytest.mark.asyncio
async def test_hot_done_gap_is_claimed_before_a_citation_but_not_when_it_is_the_only_row(db, monkeypatch):
    """The 6h cooldown still idles a hot gap when nothing else is due.

    A due size-0 citation must not use that idle window to cut ahead of SCMR|2025.
    """
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    hot = _year(status="done", gap_size=263, last_run_at=now - timedelta(minutes=5))
    db.add(hot)
    await db.commit()

    assert await _claim(db) is None

    db.add(_citation(_slot1_citation_key(), when=now - timedelta(hours=3)))
    await db.commit()

    row = await _claim(db)
    assert row is not None
    assert row.query_key == "search:reporter=SCMR|year=2025"


@pytest.mark.asyncio
async def test_gap_focus_off_prefers_larger_gap_over_an_older_higher_priority_row(db, monkeypatch):
    _enable(monkeypatch, gap_focus=False)
    now = datetime.now(timezone.utc)
    db.add(
        PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key="search:reporter=YLR|year=1980",
            query_json={"reporter": "YLR", "year": 1980},
            status="pending",
            priority=1,
            gap_size=0,
            last_run_at=None,
        )
    )
    db.add(_year(last_run_at=now - timedelta(minutes=30)))
    await db.commit()

    row = await _claim(db)
    assert row is not None
    assert row.query_key == "search:reporter=SCMR|year=2025"


@pytest.mark.asyncio
async def test_long_waiting_small_gap_is_interleaved_ahead_of_a_recent_large_gap(db, monkeypatch):
    """A smaller positive gap that has already waited out 8 cooldowns is served next.

    A never-run size-263 gap stays ahead of that small gap: interleave does not hide it.
    """
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    db.add(
        PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key="search:reporter=YLR|year=2019",
            query_json={"reporter": "YLR", "year": 2019},
            status="pending",
            priority=10,
            gap_size=2,
            last_run_at=now - timedelta(hours=49),
        )
    )
    db.add(_year(gap_size=263, last_run_at=now - timedelta(hours=1)))
    await db.commit()

    small = await _claim(db)
    assert small is not None
    assert small.query_key == "search:reporter=YLR|year=2019"

    fresh = _year(
        query_key="search:reporter=SCMR|year=2024",
        query_json={"reporter": "SCMR", "year": 2024},
        gap_size=263,
        last_run_at=None,
        status="pending",
    )
    db.add(fresh)
    await db.commit()
    # The small gap is in_progress from the claim above. A never-run larger gap
    # is still the next claim, ahead of any other long-waiting small row.
    nxt = await _claim(db)
    assert nxt is not None
    assert nxt.query_key == "search:reporter=SCMR|year=2024"


def _runner(monkeypatch, db, pages, *, known=None, preserve="staged", total=None):
    class FakePipeline:
        def __init__(self):
            self.runner = SimpleNamespace(browser=None)
            self.local_engine = None

        async def fetch_results(self, search_map, values):
            return SimpleNamespace(html="page1", url="page1")

        async def fetch_detail(self, url):
            return SimpleNamespace(html="", url=url)

        async def preserve_and_extract(self, detail, route, row):
            return preserve

        async def sleep(self, seconds):
            return None

    async def extract_result_rows(self, *, html, search_map, base_url):
        rows, nxt = pages[html]
        data = {"result_rows": rows, "next_page": nxt}
        if total is not None:
            data["total_results_if_shown"] = total
        return SimpleNamespace(data=data)

    async def load_known(db, keys):
        return set(known or ())

    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows", extract_result_rows)
    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(harvest, "unmapped_harvest_reason", lambda *a: None)
    monkeypatch.setattr(harvest, "build_harvest_form_values", lambda *a: {})
    monkeypatch.setattr(harvest, "release_page_result", lambda page: None)
    monkeypatch.setattr(harvest, "parse_total_results_from_html", lambda html: None)
    monkeypatch.setattr(harvest, "load_known_citation_keys", load_known)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db, runner.source, runner.dry_run = db, None, False
    runner.pipeline = FakePipeline()
    runner.memory = SimpleNamespace(note_window=lambda: None)
    runner.pacing_profile = {"login_delay_min": 0, "login_delay_max": 0}
    runner.spot_check_between_pages = False

    async def no_recycle():
        return None

    runner._maybe_recycle_browser = no_recycle
    return runner


def _retry_at(row: PlsSearchHarvestQuery) -> datetime:
    raw = (row.cursor_json or {}).get("harvest_retry_after")
    assert raw, row.cursor_json
    return datetime.fromisoformat(raw)


@pytest.mark.asyncio
async def test_repeated_zero_yield_is_not_immediately_reclaimed(db, monkeypatch):
    _enable(monkeypatch, tick_seconds=30)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)
    key = _slot1_citation_key()
    row = _citation(key, when=None)
    row.status = "in_progress"
    db.add(row)
    await db.commit()

    runner = _runner(monkeypatch, db, {"page1": ([], None)})
    await runner.run_query(row, {})
    assert row.rows_new == 0
    assert int((row.cursor_json or {}).get("harvest_attempts") or 0) == 0
    assert int((row.cursor_json or {}).get("harvest_empty_passes") or 0) == 1
    first_retry = _retry_at(row)
    assert first_retry > datetime.now(timezone.utc)

    await runner.run_query(row, {})
    assert int((row.cursor_json or {}).get("harvest_attempts") or 0) == 0
    assert int((row.cursor_json or {}).get("harvest_empty_passes") or 0) == 2
    second_retry = _retry_at(row)
    assert second_retry > first_retry

    row.status = "pending"
    await db.commit()
    assert await _claim(db) is None
    await db.refresh(row)
    assert row.status == "pending"
    assert int((row.cursor_json or {}).get("harvest_empty_passes") or 0) == 2
    assert int((row.cursor_json or {}).get("harvest_attempts") or 0) == 0


@pytest.mark.asyncio
async def test_nonzero_yield_clears_backoff(db, monkeypatch):
    _enable(monkeypatch)
    later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    row = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=SCMR|year=2025",
        query_json={"reporter": "SCMR", "year": 2025},
        status="in_progress",
        gap_size=263,
        rows_new=0,
        cursor_json={
            "page": 1,
            "row_index": 0,
            "harvest_attempts": 2,
            "harvest_empty_passes": 4,
            "harvest_retry_after": later,
        },
    )
    db.add(row)
    await db.commit()

    runner = _runner(
        monkeypatch,
        db,
        {"page1": ([{"citation": "2025 SCMR 1", "detail_url": "https://example.test/1"}], None)},
        known=set(),
        preserve="staged",
        total=1,
    )
    await runner.run_query(row, {})
    assert int(row.rows_new or 0) > 0
    assert row.status == "done"
    cursor = row.cursor_json or {}
    assert "harvest_retry_after" not in cursor
    assert "harvest_empty_passes" not in cursor
    assert int(cursor.get("harvest_attempts") or 0) == 2

    row.status = "pending"
    row.last_run_at = datetime.now(timezone.utc) - timedelta(hours=8)
    await db.commit()
    claimed = await _claim(db)
    assert claimed is not None
    assert claimed.query_key == row.query_key


def test_page_cursor_rebuild_keeps_lease_keys():
    cursor = {
        "page": 4,
        "row_index": 2,
        "harvest_attempts": 2,
        "harvest_empty_passes": 4,
        "harvest_retry_after": "2026-11-24T12:00:00+00:00",
        "harvest_reclaim_open": True,
        "unresolved_samples": [{"citation": "1979 SCMR 1"}],
    }
    fresh = fresh_cursor_keeping_annexure_state(cursor)
    assert fresh["page"] == 1 and fresh["row_index"] == 0
    assert fresh["harvest_attempts"] == 2
    assert fresh["harvest_empty_passes"] == 4
    assert fresh["harvest_retry_after"] == "2026-11-24T12:00:00+00:00"
    assert fresh["harvest_reclaim_open"] is True
    assert fresh["unresolved_samples"][0]["citation"] == "1979 SCMR 1"


@pytest.mark.asyncio
async def test_empty_passes_do_not_exhaust_the_failure_budget(db, monkeypatch):
    """Five empty passes must not make the next stale-lease error mark SCMR|2025 failed."""
    _enable(monkeypatch, tick_seconds=30)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)
    row = _year(status="in_progress", gap_size=263, rows_new=0)
    db.add(row)
    await db.commit()

    runner = _runner(monkeypatch, db, {"page1": ([], None)})
    for _ in range(5):
        await runner.run_query(row, {})
    assert int((row.cursor_json or {}).get("harvest_empty_passes") or 0) == 5
    assert int((row.cursor_json or {}).get("harvest_attempts") or 0) == 0
    assert _retry_at(row) > datetime.now(timezone.utc)

    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 30)
    row.status = "in_progress"
    row.last_error = "pagination interrupted: timeout"
    row.last_run_at = datetime.now(timezone.utc) - timedelta(seconds=30 * 2) - timedelta(seconds=5)
    await db.commit()

    await _claim(db)
    await db.refresh(row)
    assert row.status != "failed"
    assert int((row.cursor_json or {}).get("harvest_attempts") or 0) == 1
    assert int((row.cursor_json or {}).get("harvest_empty_passes") or 0) == 5
    assert "exhausted" not in (row.last_error or "")


def test_stale_lease_reclaim_keeps_skip_locked():
    source = inspect.getsource(harvest.reclaim_stale_harvest_queries)
    assert "skip_locked=True" in source
