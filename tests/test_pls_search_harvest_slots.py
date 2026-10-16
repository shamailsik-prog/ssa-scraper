"""Owner decision 16 October 2026: Account 1 owns all PakistanLawSite acquisition and Account 2 is
reserved for independent verification. Search-harvest rows remain lock-safe and resumable, but are no longer
partitioned across accounts and acquisition never takes over the verifier account."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.auth.session_manager import SessionLock
from scraper.config import KNOWN_REPORTERS, settings
from scraper.database import SessionLocal
from scraper.models import BrowserSessionSlot, PlsSearchHarvestQuery, ScraperSource
from scraper.pls_search_harvest_core import search_harvest_slot_for, slot1_reporter_share
from scraper.tasks import pls_search_harvest as harvest
from scraper.tasks import spot_check

SLOT1 = "PLD,SCMR,CLC,YLR,CLD,GBLR"


@pytest_asyncio.fixture
async def r(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", SLOT1)
    client = aioredis.from_url(settings.REDIS_URL)

    async def clear():
        keys = await client.keys("corpus:login_session_lock:PakistanLawSite*")
        keys += await client.keys(spot_check.SPOT_CHECK_TURN_KEY)
        if keys:
            await client.delete(*keys)

    await clear()
    yield client
    await clear()
    await client.aclose()


async def _seed(db, reporters, *, priority=100):
    rows = []
    for rep in reporters:
        row = PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key=f"search:reporter={rep}|year=2026",
            query_json={"reporter": rep, "year": 2026},
            status="pending",
            priority=priority,
        )
        db.add(row)
        rows.append(row)
    await db.commit()
    return rows


async def _slots(db, *, down=None, down_for=timedelta(0)):
    """Both logins ACTIVE, or `down` logged out since `down_for` ago."""
    now = datetime.now(timezone.utc)
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    source.state = "ACTIVE"
    for row in (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite"))).scalars().all():
        if row.slot_number == down:
            row.state = "NEEDS_HUMAN_LOGIN"
            row.last_used_at = row.last_verified_at = now - down_for
            row.logged_in_at = None
        elif row.slot_number in (1, 2):
            row.state = "ACTIVE"
            row.storage_state_encrypted = settings.encrypt_value("{}")
    await db.commit()


def _fake_tick(monkeypatch, *, hold=0.0):
    """run_search_harvest with everything that would open the site replaced: the run records the login it was
    bound to, the row it worked and when, and marks the row done."""
    spans = []

    async def run_query(self, query_row, search_map, *, max_pages=0):
        start = time.monotonic()
        await asyncio.sleep(hold)
        spans.append(
            {
                "slot": self.pipeline.runner.preferred_slot_number,
                "exclusive": self.pipeline.runner.exclusive_slot,
                "reporter": query_row.query_json["reporter"],
                "start": start,
                "end": time.monotonic(),
            }
        )
        query_row.status = "done"
        return {"query_key": query_row.query_key, "status": "done"}

    async def active_map(db, source_name):
        return object()

    monkeypatch.setattr(harvest.SearchHarvestRunner, "run_query", run_query)
    monkeypatch.setattr(harvest, "active_map", active_map)
    monkeypatch.setattr(harvest, "map_as_dict", lambda m: {})
    monkeypatch.setattr(harvest.PakistanLawSitePipeline, "_is_dashboard_citation_fields", classmethod(lambda cls, m: True))
    monkeypatch.setattr(settings, "LOGIN_SESSION_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "BACKFILL_LOGIN_SESSION_CONCURRENCY", 2)
    return spans


async def _tick(slot, r):
    async with SessionLocal() as s:
        return await harvest.run_search_harvest(s, slot=slot, redis_client=r)


async def test_account1_claims_the_complete_search_ledger_and_account2_claims_none(db, r):
    await _seed(db, ["PLD", "SCMR", "PLC", "PCrLJ", "MLD"])
    claimed = []
    locks = []
    while True:
        row, lock = await harvest.claim_next_query(db, 1, redis_client=r)
        if row is None:
            break
        claimed.append(row.query_json["reporter"])
        locks.append(lock)
    assert sorted(claimed) == ["MLD", "PCrLJ", "PLC", "PLD", "SCMR"]
    row2, lock2 = await harvest.claim_next_query(db, 2, redis_client=r)
    assert row2 is None and lock2 is None
    for lock in locks:
        await lock.release()


async def test_account2_search_harvest_is_refused_even_when_active(db, r, monkeypatch):
    await _slots(db)
    await _seed(db, ["PLD"])
    spans = _fake_tick(monkeypatch)
    result = await _tick(2, r)
    assert result["skipped"] is True
    assert "reserved for independent verification" in result["reason"]
    assert spans == []


async def test_account1_tick_remains_singleton(db, r, monkeypatch):
    await _slots(db)
    await _seed(db, ["PLD"])
    spans = _fake_tick(monkeypatch)
    running = SessionLock("PakistanLawSite:search_harvest:slot1", r, max_holders=1)
    await running.acquire()
    try:
        result = await _tick(1, r)
    finally:
        await running.release()
    assert result["skipped"] is True and "already running on slot 1" in result["reason"]
    assert spans == []


async def test_account1_unavailable_never_borrows_verifier_account(db, r, monkeypatch):
    await _slots(db, down=1, down_for=timedelta(minutes=60))
    await _seed(db, ["PLC"])
    spans = _fake_tick(monkeypatch)
    result = await _tick(1, r)
    assert result["skipped"] is True
    assert "harvester slot 1" in result["reason"]
    assert "verifier slot is never used" in result["reason"]
    assert spans == []


async def test_rows_are_picked_by_priority_then_by_the_oldest_last_run(db):
    now = datetime.now(timezone.utc)
    a, b, c, d = await _seed(db, ["A", "B", "C", "D"])
    a.last_run_at, b.last_run_at, c.last_run_at = now - timedelta(hours=1), None, now - timedelta(hours=2)
    d.priority, d.last_run_at = 50, now
    await db.commit()
    order = []
    while (row := await harvest.pick_next_query(db, priority_gaps=True)) is not None:
        order.append(row.query_json["reporter"])
        row.status = "done"
        await db.flush()
    assert order == ["D", "B", "C", "A"]


# ------------------------------------------------------------------ run_query: dedupe, priority, spot check
def _row(n):
    return {"citation": f"2001 PLC {n}", "detail_url": f"https://example.test/case/{n}"}


def _runner(db, monkeypatch, pages, *, total=None):
    class FakePipeline:
        def __init__(self):
            self.fetched_details = []
            self.runner = SimpleNamespace(browser=None)
            self.local_engine = None
            self.redis_client = None

        async def fetch_results(self, search_map, values):
            return SimpleNamespace(html="page1", url="page1")

        async def fetch_detail(self, url):
            if url in pages:
                return SimpleNamespace(html=url, url=url)
            self.fetched_details.append(url)
            return SimpleNamespace(html="", url=url)

        async def preserve_and_extract(self, detail, route, row):
            return "staged"

        async def sleep(self, seconds):
            return None

    async def extract_result_rows(self, *, html, search_map, base_url):
        rows, nxt = pages[html]
        return SimpleNamespace(data={"result_rows": rows, "next_page": nxt, "total_results_if_shown": total})

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


async def _query(db, priority=105):
    q = PlsSearchHarvestQuery(source_name="PakistanLawSite", query_key="search:reporter=PLC|year=2001", query_json={"reporter": "PLC", "year": 2001}, status="in_progress", priority=priority)
    db.add(q)
    await db.commit()
    return q


async def test_rows_a_later_page_repeats_are_neither_opened_nor_counted_again(db, monkeypatch):
    pages = {"page1": ([_row(1), _row(2)], "page2"), "page2": ([_row(1), _row(2), _row(3)], None)}
    q = await _query(db)
    runner = _runner(db, monkeypatch, pages)
    await runner.run_query(q, {})
    assert runner.pipeline.fetched_details == [f"https://example.test/case/{n}" for n in (1, 2, 3)]
    assert q.rows_seen == 3 and q.rows_new == 3


async def test_pagination_that_keeps_serving_old_rows_stops_and_says_why(db, monkeypatch):
    pages = {
        "page1": ([_row(1), _row(2)], "page2"),
        "page2": ([_row(1), _row(2)], "page3"),
        "page3": ([_row(1), _row(2)], "page4"),
        "page4": ([_row(1), _row(2)], "page5"),
    }
    q = await _query(db)
    runner = _runner(db, monkeypatch, pages)
    await runner.run_query(q, {})
    assert runner.pipeline.fetched_details == ["https://example.test/case/1", "https://example.test/case/2"]
    assert q.status == "in_progress" and "repeated rows of earlier pages" in (q.last_error or "")
    assert q.rows_seen == 2


async def test_a_row_with_an_unknown_site_total_keeps_its_priority(db, monkeypatch):
    pages = {"page1": ([_row(1), _row(2)], None)}
    q = await _query(db, priority=105)
    runner = _runner(db, monkeypatch, pages, total=None)
    await runner.run_query(q, {})
    assert q.site_total_results is None and q.priority == 105  # was set to 1000 - 0 = 1000, the back of the ledger

    q2 = PlsSearchHarvestQuery(source_name="PakistanLawSite", query_key="known", query_json={"reporter": "PLC"}, status="in_progress", priority=105)
    db.add(q2)
    await db.commit()
    runner = _runner(db, monkeypatch, {"page1": ([_row(5), _row(6)], "page2"), "page2": ([_row(7)], None)}, total=9)
    await runner.run_query(q2, {})
    assert q2.site_total_results == 9 and q2.priority == 1000 - q2.gap_size  # a measured gap still orders the row


async def test_search_harvest_never_substitutes_for_account2_verification(db, monkeypatch, r):
    pages = {"page1": ([_row(1)], "page2"), "page2": ([_row(2)], None)}
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("harvester must not perform verifier work")

    monkeypatch.setattr(spot_check, "check_judgments_between_pages", forbidden)
    q = await _query(db)
    runner = _runner(db, monkeypatch, pages)
    runner.spot_check_between_pages = False
    runner.pipeline.redis_client = r
    await runner.run_query(q, {})
    assert calls == []
    assert await spot_check.claim_spot_check_turn(r) is True



def test_a_tick_that_worked_queues_the_next_tick_for_its_own_slot(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CONTINUE", True)
    assert harvest._continue_slot({"query_key": "k", "status": "in_progress", "slot": 2}) == 2
    assert harvest._continue_slot({"skipped": True, "slot": 1}) is None
    assert harvest._continue_slot({"paused": True, "query_key": "k", "slot": 1}) is None
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CONTINUE", False)
    assert harvest._continue_slot({"query_key": "k", "status": "done", "slot": 1}) is None

    sent = []
    app = SimpleNamespace(send_task=lambda name, kwargs=None, queue=None, **extra: sent.append((name, kwargs, queue, extra)))
    assert harvest.queue_search_harvest_ticks(app) == [1]
    assert sent == [
        ("scraper.tasks.pls_search_harvest.pls_search_harvest_tick", {"slot": 1}, "login_session", {}),
    ]
    assert harvest.queue_search_harvest_ticks(app, slots=(2,)) == []
