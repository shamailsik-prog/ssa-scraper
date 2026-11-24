"""Citation gap-0 rows with slot NULL must be claimable, and a stale in_progress row must be leased back.

Production (GAP_FOCUS on, 22 Nov 2026): about 1,681 pending `search:citation=` rows sat at gap 0
with slot NULL, and every pls_search_harvest_tick skipped with "no pending search harvest query
in slot N's share". CLD|2026 and PLC|2026 were left in_progress after "pagination ended without
next link and site total unknown" and were never reclaimed.

These tests fail while claim excludes that backlog and while a stuck in_progress row is invisible
until the 6-hour gap-0 cooldown. Removing the NULL-slot citation fallback makes the citation
tests fail. Removing the lease reclaim makes the stale in_progress tests fail.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.auth.session_manager import SessionLock
from scraper.config import settings
from scraper.database import SessionLocal
from scraper.models import BrowserSessionSlot, PlsSearchHarvestQuery, ScraperSource
from scraper.pls_search_harvest_core import citation_backlog_slot
from scraper.tasks import pls_search_harvest as harvest

SLOT1 = "PLD,SCMR,CLC,YLR,CLD,GBLR"
UNKNOWN_TOTAL = "pagination ended without next link and site total unknown"


def _citation_slot(query_key: str) -> int:
    """Stable owner of a no-reporter citation row: md5 first byte mod the two login slots.

    Duplicated here so a drift in citation_backlog_slot fails the partition test.
    """
    return 1 + (hashlib.md5(query_key.encode("utf-8")).digest()[0] % 2)


def _enable(monkeypatch, *, tick_seconds: int = 30) -> None:
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_GAP_FOCUS", True)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", SLOT1)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", tick_seconds)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", False)


def _lease(tick_seconds: int = 30) -> timedelta:
    return timedelta(seconds=tick_seconds * 2)


def _citation(query_key: str, citation: str) -> PlsSearchHarvestQuery:
    return PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key=query_key,
        query_json={"citation": citation},
        status="pending",
        priority=200,
        gap_size=0,
        slot=None,
    )


def _keys_covering_both_slots() -> list[str]:
    found: dict[int, list[str]] = {1: [], 2: []}
    n = 0
    while len(found[1]) < 2 or len(found[2]) < 2:
        key = f"search:citation=2099 SCMR {n}"
        found[_citation_slot(key)].append(key)
        n += 1
    return found[1][:2] + found[2][:2]


@pytest_asyncio.fixture
async def r():
    client = aioredis.from_url(settings.REDIS_URL)

    async def clear():
        keys = await client.keys("corpus:login_session_lock:PakistanLawSite*")
        if keys:
            await client.delete(*keys)

    await clear()
    yield client
    await clear()
    await client.aclose()


async def _activate_slots(db):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))
    ).scalars().first()
    source.state = "ACTIVE"
    now = datetime.now(timezone.utc)
    for row in (
        await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite"))
    ).scalars().all():
        if row.slot_number in (1, 2):
            row.state = "ACTIVE"
            row.last_used_at = now
            row.storage_state_encrypted = settings.encrypt_value("{}")
    await db.commit()


@pytest.mark.asyncio
async def test_gap_focus_claims_citation_gap0_null_slot_instead_of_skipping(db, r, monkeypatch):
    """All pending rows are citation, gap 0, slot NULL: the tick must not skip its share."""
    _enable(monkeypatch)
    key = "search:citation=2026 CLD 1"
    assert citation_backlog_slot(key) == _citation_slot(key)
    owner = _citation_slot(key)
    db.add(_citation(key, "2026 CLD 1"))
    await _activate_slots(db)

    async def no_map(_db, _source_name):
        return None

    monkeypatch.setattr(harvest, "active_map", no_map)

    async with SessionLocal() as session:
        result = await harvest.run_search_harvest(session, slot=owner, redis_client=r)
    assert result.get("skipped") is True
    assert "no pending search harvest query" not in (result.get("reason") or "")
    assert result.get("slot") == owner
    assert "no active search form map" in (result.get("reason") or "")

    other = 2 if owner == 1 else 1
    async with SessionLocal() as session:
        other_result = await harvest.run_search_harvest(session, slot=other, redis_client=r)
    assert other_result.get("skipped") is True
    assert f"slot {other}'s share" in (other_result.get("reason") or "")


@pytest.mark.asyncio
async def test_citation_gap0_null_slots_are_partitioned_and_never_double_claimed(db, r, monkeypatch):
    _enable(monkeypatch)
    keys = _keys_covering_both_slots()
    for key in keys:
        db.add(_citation(key, key.removeprefix("search:citation=")))
    await db.commit()

    taken: dict[int, list] = {1: [], 2: []}
    locks = []
    for slot in (1, 2):
        while True:
            row, lock = await harvest.claim_next_query(db, slot, redis_client=r)
            if row is None:
                break
            assert row.slot == slot
            assert row.status == "in_progress"
            assert _citation_slot(row.query_key) == slot
            taken[slot].append(row.id)
            locks.append(lock)
    assert taken[1] and taken[2]
    every = taken[1] + taken[2]
    assert len(every) == len(keys) and len(set(every)) == len(keys)
    for lock in locks:
        await lock.release()


@pytest.mark.asyncio
async def test_gap_focus_still_prefers_a_positive_gap_over_the_citation_backlog(db, monkeypatch):
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    citation_key = "search:citation=1979 SCMR 625"
    db.add(_citation(citation_key, "1979 SCMR 625"))
    db.add(
        PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key="search:reporter=PLC|year=2026",
            query_json={"reporter": "PLC", "year": 2026},
            status="pending",
            gap_size=40,
            priority=900,
            slot=None,
            last_run_at=now - timedelta(hours=8),
        )
    )
    await db.commit()

    row, lock = await harvest.claim_next_query(db, 2, redis_client=None)
    try:
        assert row is not None
        assert row.query_key == "search:reporter=PLC|year=2026"
    finally:
        if lock is not None:
            await lock.release()


@pytest.mark.asyncio
async def test_stale_in_progress_is_reclaimed_and_the_attempt_is_counted(db, monkeypatch):
    _enable(monkeypatch, tick_seconds=30)
    stale = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2026",
        query_json={"reporter": "CLD", "year": 2026},
        status="in_progress",
        gap_size=0,
        slot=None,
        priority=100,
        last_error=UNKNOWN_TOTAL,
        last_run_at=datetime.now(timezone.utc) - _lease(30) - timedelta(seconds=5),
        cursor_json={"page": 2, "row_index": 0},
    )
    db.add(stale)
    await db.commit()

    row, lock = await harvest.claim_next_query(db, 1, redis_client=None)
    try:
        assert row is not None
        assert row.query_key == stale.query_key
        assert row.status == "in_progress"
        assert row.slot == 1
        assert int((row.cursor_json or {}).get("harvest_attempts") or 0) == 1
        assert (row.cursor_json or {}).get("page") == 2
        assert UNKNOWN_TOTAL in (row.last_error or "")
    finally:
        if lock is not None:
            await lock.release()


@pytest.mark.asyncio
async def test_in_progress_inside_the_lease_is_not_reclaimed(db, monkeypatch):
    _enable(monkeypatch, tick_seconds=30)
    fresh = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2026",
        query_json={"reporter": "CLD", "year": 2026},
        status="in_progress",
        gap_size=0,
        slot=1,
        last_error=UNKNOWN_TOTAL,
        last_run_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        cursor_json={"page": 1},
    )
    db.add(fresh)
    await db.commit()

    row, lock = await harvest.claim_next_query(db, 1, redis_client=None)
    assert row is None and lock is None
    await db.refresh(fresh)
    assert fresh.status == "in_progress"
    assert "harvest_attempts" not in (fresh.cursor_json or {})


@pytest.mark.asyncio
async def test_live_query_lock_blocks_lease_reclaim(db, r, monkeypatch):
    _enable(monkeypatch, tick_seconds=30)
    stale = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2026",
        query_json={"reporter": "CLD", "year": 2026},
        status="in_progress",
        gap_size=0,
        slot=1,
        last_error=UNKNOWN_TOTAL,
        last_run_at=datetime.now(timezone.utc) - _lease(30) - timedelta(seconds=5),
        cursor_json={"page": 1},
    )
    db.add(stale)
    await db.commit()
    held = SessionLock(f"PakistanLawSite:search_harvest:query:{stale.id}", r, max_holders=1)
    await held.acquire()
    try:
        row, lock = await harvest.claim_next_query(db, 1, redis_client=r)
        assert row is None and lock is None
        await db.refresh(stale)
        assert stale.status == "in_progress"
        assert "harvest_attempts" not in (stale.cursor_json or {})
    finally:
        await held.release()


@pytest.mark.asyncio
async def test_repeated_failures_back_off_then_stop(db, monkeypatch):
    _enable(monkeypatch, tick_seconds=30)
    monkeypatch.setattr(harvest, "search_harvest_max_attempts", lambda: 3, raising=False)
    cooling = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLC|year=2026",
        query_json={"reporter": "PLC", "year": 2026},
        status="in_progress",
        gap_size=0,
        slot=2,
        last_error=UNKNOWN_TOTAL,
        last_run_at=datetime.now(timezone.utc) - _lease(30) - timedelta(seconds=5),
        cursor_json={"page": 1, "harvest_attempts": 1},
    )
    db.add(cooling)
    await db.commit()

    row, lock = await harvest.claim_next_query(db, 2, redis_client=None)
    assert row is None and lock is None
    await db.refresh(cooling)
    assert cooling.status == "pending"
    assert int((cooling.cursor_json or {}).get("harvest_attempts") or 0) == 2
    retry_after = (cooling.cursor_json or {}).get("harvest_retry_after")
    assert retry_after
    assert datetime.fromisoformat(retry_after) > datetime.now(timezone.utc)

    cooling.status = "in_progress"
    cooling.last_run_at = datetime.now(timezone.utc) - _lease(30) - timedelta(seconds=5)
    cursor = dict(cooling.cursor_json or {})
    cursor.pop("harvest_retry_after", None)
    cursor["harvest_attempts"] = 2
    cooling.cursor_json = cursor
    await db.commit()

    again, again_lock = await harvest.claim_next_query(db, 2, redis_client=None)
    assert again is None and again_lock is None
    await db.refresh(cooling)
    assert cooling.status == "failed"
    assert int((cooling.cursor_json or {}).get("harvest_attempts") or 0) == 3
    assert "exhausted" in (cooling.last_error or "")


def _repeat_runner(db, monkeypatch, pages):
    """Pages that repeat earlier rows, so the run rebuilds the cursor between pages."""

    class FakePipeline:
        def __init__(self):
            self.runner = SimpleNamespace(browser=None)
            self.local_engine = None

        async def fetch_results(self, search_map, values):
            return SimpleNamespace(html="page1", url="page1")

        async def fetch_detail(self, url):
            if url in pages:
                return SimpleNamespace(html=url, url=url)
            return SimpleNamespace(html="", url=url)

        async def preserve_and_extract(self, detail, route, row):
            return "staged"

        async def sleep(self, seconds):
            return None

    async def extract_result_rows(self, *, html, search_map, base_url):
        rows, nxt = pages[html]
        return SimpleNamespace(data={"result_rows": rows, "next_page": nxt})

    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows", extract_result_rows)
    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(harvest, "unmapped_harvest_reason", lambda *a: None)
    monkeypatch.setattr(harvest, "build_harvest_form_values", lambda *a: {})
    monkeypatch.setattr(harvest, "release_page_result", lambda page: None)
    monkeypatch.setattr(harvest, "parse_total_results_from_html", lambda html: None)
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


def _result_row(n: int) -> dict:
    return {"citation": f"2019 CLD {n}", "detail_url": f"https://example.test/lease/{n}"}


@pytest.mark.asyncio
async def test_repeated_page_run_keeps_attempts_so_backoff_and_cap_still_apply(db, monkeypatch):
    """A multi-page rebuild must not wipe the lease count.

    After pages repeat, a row already on its second failure still backs off, and a
    row already at the cap is failed instead of starting again at attempt 1.
    """
    _enable(monkeypatch, tick_seconds=30)
    monkeypatch.setattr(harvest, "search_harvest_max_attempts", lambda: 3, raising=False)
    past = "2020-01-01T00:00:00+00:00"
    pages = {
        "page1": ([_result_row(1), _result_row(2)], "page2"),
        "page2": ([_result_row(1), _result_row(2)], "page3"),
        "page3": ([_result_row(1), _result_row(2)], "page4"),
    }
    backing_off = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2024",
        query_json={"reporter": "CLD", "year": 2024},
        status="in_progress",
        gap_size=0,
        slot=1,
        priority=80,
        cursor_json={"page": 1, "row_index": 0, "harvest_attempts": 1, "harvest_retry_after": past, "harvest_reclaim_open": True},
    )
    at_cap = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=YLR|year=2024",
        query_json={"reporter": "YLR", "year": 2024},
        status="in_progress",
        gap_size=0,
        slot=1,
        priority=70,
        cursor_json={"page": 1, "row_index": 0, "harvest_attempts": 2, "harvest_retry_after": past, "harvest_reclaim_open": True},
    )
    db.add(backing_off)
    db.add(at_cap)
    await db.commit()

    runner = _repeat_runner(db, monkeypatch, pages)
    await runner.run_query(backing_off, {})
    await runner.run_query(at_cap, {})
    assert "repeated rows of earlier pages" in (backing_off.last_error or "")
    assert "repeated rows of earlier pages" in (at_cap.last_error or "")
    assert int((backing_off.cursor_json or {}).get("harvest_attempts") or 0) == 1
    assert (backing_off.cursor_json or {}).get("harvest_retry_after") == past
    assert (backing_off.cursor_json or {}).get("harvest_reclaim_open") is True
    assert int((at_cap.cursor_json or {}).get("harvest_attempts") or 0) == 2

    stale = datetime.now(timezone.utc) - _lease(30) - timedelta(seconds=5)
    backing_off.last_run_at = stale
    at_cap.last_run_at = stale
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 30)
    await db.commit()

    row, lock = await harvest.claim_next_query(db, 1, redis_client=None)
    assert row is None and lock is None
    await db.refresh(backing_off)
    await db.refresh(at_cap)
    assert backing_off.status == "pending"
    assert int((backing_off.cursor_json or {}).get("harvest_attempts") or 0) == 2
    retry_after = (backing_off.cursor_json or {}).get("harvest_retry_after")
    assert retry_after and datetime.fromisoformat(retry_after) > datetime.now(timezone.utc)
    assert at_cap.status == "failed"
    assert int((at_cap.cursor_json or {}).get("harvest_attempts") or 0) == 3
    assert "exhausted" in (at_cap.last_error or "")


@pytest.mark.asyncio
async def test_cooling_positive_gap_does_not_block_a_due_gap0_row(db, monkeypatch):
    """A positive-gap row still inside harvest_retry_after must not hide a due gap-0 row."""
    _enable(monkeypatch)
    later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    cooling = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017",
        query_json={"reporter": "PLD", "year": 2017},
        status="pending",
        gap_size=40,
        slot=1,
        priority=900,
        last_run_at=datetime.now(timezone.utc) - timedelta(hours=2),
        cursor_json={"harvest_attempts": 2, "harvest_retry_after": later, "harvest_reclaim_open": True},
    )
    due = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=YLR|year=2024",
        query_json={"reporter": "YLR", "year": 2024},
        status="pending",
        gap_size=0,
        slot=None,
        priority=10,
        last_run_at=datetime.now(timezone.utc) - timedelta(hours=8),
        cursor_json={"page": 1},
    )
    db.add(cooling)
    db.add(due)
    await db.commit()

    row, lock = await harvest.claim_next_query(db, 1, redis_client=None)
    try:
        assert row is not None
        assert row.query_key == due.query_key
        assert row.status == "in_progress"
    finally:
        if lock is not None:
            await lock.release()
    await db.refresh(cooling)
    assert cooling.status == "pending"
    assert cooling.query_key != (row.query_key if row is not None else None)


@pytest.mark.asyncio
async def test_two_slots_cannot_both_reclaim_or_overwrite_one_stale_row(db, r, monkeypatch):
    """Two sessions that both see one expired lease must not both write it.

    The second session loads the row, waits until the first has claimed and committed,
    then flushes. Without a row lock that flush puts the claimed row back to pending.
    """
    _enable(monkeypatch, tick_seconds=30)
    stale = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2025",
        query_json={"reporter": "CLD", "year": 2025},
        status="in_progress",
        gap_size=0,
        slot=None,
        priority=50,
        last_error=UNKNOWN_TOTAL,
        last_run_at=datetime.now(timezone.utc) - _lease(30) - timedelta(seconds=5),
        cursor_json={"page": 2, "row_index": 0},
    )
    db.add(stale)
    await db.commit()
    row_id = stale.id

    second_loaded_or_skipped = asyncio.Event()
    first_committed = asyncio.Event()
    state = {"calls": 0}
    real_held = harvest._query_lock_held

    async def held(row_id_arg, redis_client):
        if await real_held(row_id_arg, redis_client):
            return True
        state["calls"] += 1
        if state["calls"] == 1:
            try:
                await asyncio.wait_for(second_loaded_or_skipped.wait(), timeout=5)
            except asyncio.TimeoutError:
                return False
            return False
        second_loaded_or_skipped.set()
        await asyncio.wait_for(first_committed.wait(), timeout=5)
        return False

    monkeypatch.setattr(harvest, "_query_lock_held", held)

    async def claim(slot: int):
        async with SessionLocal() as session:
            row, lock = await harvest.claim_next_query(session, slot, redis_client=r, take_over=True)
            if row is None:
                second_loaded_or_skipped.set()
            else:
                first_committed.set()
            return row, lock

    try:
        (first_row, first_lock), (second_row, second_lock) = await asyncio.gather(claim(1), claim(2))
        winners = [row for row in (first_row, second_row) if row is not None]
        assert len(winners) == 1
        async with SessionLocal() as check:
            found = (await check.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.id == row_id))).scalar_one()
        assert found.status == "in_progress"
        assert found.slot in (1, 2)
        assert int((found.cursor_json or {}).get("harvest_attempts") or 0) == 1
        assert found.last_error == UNKNOWN_TOTAL
    finally:
        for lock in (locals().get("first_lock"), locals().get("second_lock")):
            if lock is not None:
                await lock.release()
