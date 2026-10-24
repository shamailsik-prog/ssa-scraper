"""Owner's decision of 13 October 2026 (handbook R3.1, R3.7): the PakistanLawSite search harvest runs on login 1 and
login 2 at the same time, each on its own fixed share of the reporters, with no overlap. Each slot has its own
singleton lock instead of one global "another search harvest is running" lock; ledger rows are taken with
SELECT ... FOR UPDATE SKIP LOCKED and held by a per-row lock, so two ticks never work the same row; a slot logged out
for 30 minutes has its share taken over by the other. A row whose site total is unknown keeps its priority (it used
to sink to 1000), rows a later page repeats are neither opened nor counted again, and the 30-minute spot check runs
between harvest pages instead of being skipped on the login lock."""

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
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TAKEOVER_MINUTES", 30)
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


def test_the_two_shares_never_overlap_and_cover_every_reporter():
    share = slot1_reporter_share(SLOT1)
    assert share == {"PLD", "SCMR", "CLC", "YLR", "CLD", "GBLR"}
    for rep in ("PLD", "SCMR", "CLC", "YLR", "CLD", "GBLR", "P.L.D.", "scmr"):
        assert search_harvest_slot_for({"reporter": rep}, share) == 1, rep
    for rep in ("PLC", "PLC(CS)", "PCrLJ", "P.Cr.L.J", "MLD", "PTD", "PLJ", "NLR"):
        assert search_harvest_slot_for({"reporter": rep}, share) == 2, rep
    assert search_harvest_slot_for({"keyword": "bail"}, share) == 2  # no reporter: slot 2 takes it
    # every reporter of the dropdown is in exactly one share
    assert {search_harvest_slot_for({"reporter": rep}, share) for rep in KNOWN_REPORTERS} <= {1, 2}


async def test_each_slot_claims_only_rows_of_its_own_share(db, r):
    await _seed(db, ["PLD", "SCMR", "PLC", "PCrLJ", "MLD"])
    taken = {1: [], 2: []}
    locks = []
    for slot in (1, 2):
        while True:
            row, lock = await harvest.claim_next_query(db, slot, redis_client=r)
            if row is None:
                break
            assert row.status == "in_progress" and row.slot == slot
            taken[slot].append(row.query_json["reporter"])
            locks.append(lock)
    assert sorted(taken[1]) == ["PLD", "SCMR"]
    assert sorted(taken[2]) == ["MLD", "PCrLJ", "PLC"]
    for lock in locks:
        await lock.release()


async def test_two_ticks_picking_at_the_same_moment_never_take_the_same_row(db, r):
    """Both pickers compete for one pool (as when one slot has taken over the other's share)."""
    await _seed(db, [f"R{n}" for n in range(12)])
    claimed = {1: [], 2: []}
    locks = []

    async def picker(slot):
        async with SessionLocal() as s:
            while True:
                row, lock = await harvest.claim_next_query(s, slot, redis_client=r, take_over=True)
                if row is None:
                    return
                claimed[slot].append(row.id)
                locks.append(lock)
                await asyncio.sleep(0)

    await asyncio.gather(picker(1), picker(2))
    every = claimed[1] + claimed[2]
    assert len(every) == 12 and len(set(every)) == 12  # each row taken once, none twice
    for lock in locks:
        await lock.release()


async def test_a_row_held_by_a_running_tick_is_not_taken_after_that_tick_commits(db, r):
    """The run commits after every judgment, which ends the SELECT ... FOR UPDATE row lock; the per-row lock keeps
    the row out of the other slot's reach until the run ends."""
    await _seed(db, ["PLD"])
    row, lock = await harvest.claim_next_query(db, 1, redis_client=r, take_over=True)
    assert row is not None
    await db.commit()
    async with SessionLocal() as other:
        again, _ = await harvest.claim_next_query(other, 2, redis_client=r, take_over=True)
        assert again is None
    await lock.release()
    async with SessionLocal() as other:
        again, lock2 = await harvest.claim_next_query(other, 2, redis_client=r, take_over=True)
        assert again is not None and again.id == row.id
        await lock2.release()


async def test_both_slots_harvest_at_the_same_time_each_on_its_own_share(db, r, monkeypatch):
    await _slots(db)
    await _seed(db, ["PLD", "PLC"])
    spans = _fake_tick(monkeypatch, hold=0.5)

    first, second = await asyncio.gather(_tick(1, r), _tick(2, r))

    assert first["slot"] == 1 and second["slot"] == 2, (first, second)
    assert not first.get("skipped") and not second.get("skipped")
    by_slot = {s["slot"]: s for s in spans}
    assert by_slot[1]["reporter"] == "PLD" and by_slot[2]["reporter"] == "PLC"
    assert by_slot[1]["exclusive"] and by_slot[2]["exclusive"]  # each bound to its own login only
    # the two runs overlapped in time
    assert max(s["start"] for s in spans) < min(s["end"] for s in spans)
    rows = (await db.execute(select(PlsSearchHarvestQuery).order_by(PlsSearchHarvestQuery.query_key))).scalars().all()
    await db.refresh(rows[0]), await db.refresh(rows[1])
    assert {(q.query_json["reporter"], q.slot, q.status) for q in rows} == {("PLD", 1, "done"), ("PLC", 2, "done")}


async def test_a_second_tick_on_the_same_slot_exits_at_once(db, r, monkeypatch):
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
    # slot 2 is not held up by slot 1's lock (the old global lock stopped it)
    assert (await _tick(2, r)).get("skipped") is True  # nothing in slot 2's share, but it did not wait on slot 1


async def test_a_logged_out_slot_hands_its_share_over_after_30_minutes(db, r, monkeypatch):
    await _slots(db, down=2, down_for=timedelta(minutes=10))
    await _seed(db, ["PLD", "PLC"])
    spans = _fake_tick(monkeypatch)

    down = await _tick(2, r)
    assert down["skipped"] is True and "slot 2 is NEEDS_HUMAN_LOGIN" in down["reason"]

    own = await _tick(1, r)
    assert own["slot"] == 1 and own["took_over"] is False and spans[-1]["reporter"] == "PLD"
    early = await _tick(1, r)
    assert early["skipped"] is True and "slot 1's share" in early["reason"]  # PLC waits: slot 2 down only 10 min

    await _slots(db, down=2, down_for=timedelta(minutes=31))
    late = await _tick(1, r)
    assert late["slot"] == 1 and late["took_over"] is True
    assert spans[-1] == {**spans[-1], "slot": 1, "reporter": "PLC"}


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
    runner.probe_mode = False  # __new__ fixture bypasses constructor's default
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


async def test_the_spot_check_runs_between_pages_once_per_interval(db, monkeypatch, r):
    pages = {"page1": ([_row(1)], "page2"), "page2": ([_row(2)], "page3"), "page3": ([_row(3)], None)}
    calls = []

    async def between_pages(db_, pipeline, *, sample=None):
        calls.append(pipeline)
        return [{"label": "PLD 2020 SC 1", "result": "match"}]

    monkeypatch.setattr(spot_check, "check_judgments_between_pages", between_pages)
    q = await _query(db)
    runner = _runner(db, monkeypatch, pages)
    runner.spot_check_between_pages = True
    runner.pipeline.redis_client = r
    await runner.run_query(q, {})
    assert len(calls) == 1 and calls[0] is runner.pipeline  # two page breaks, one 30-minute turn, on this login

    # the beat task finds the turn taken and does not open a second browser for it
    assert await spot_check.claim_spot_check_turn(r) is False
    await r.delete(spot_check.SPOT_CHECK_TURN_KEY)
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
    assert harvest.queue_search_harvest_ticks(app) == [1, 2]
    assert sent == [
        ("scraper.tasks.pls_search_harvest.pls_search_harvest_tick", {"slot": 1}, "login_session", {}),
        ("scraper.tasks.pls_search_harvest.pls_search_harvest_tick", {"slot": 2}, "login_session", {}),
    ]  # no expires (R3.7)
