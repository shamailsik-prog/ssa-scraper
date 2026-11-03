"""Gap-focus claim and durable unmatched-row identity.

Harvest ticks looked stalled because claim_next_query only takes pending|in_progress.
Done and split rows that still have a gap were unpickable, while open gap-0 rows
burned every tick. Unmatched CaseNames lived in logs and in cursor fields a later
lap or cursor reset could drop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from scraper.config import Settings, settings
from scraper.database import SessionLocal
from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import fresh_cursor_keeping_annexure_state, reset_lap_gap_diagnostics
from scraper.tasks import pls_search_harvest as harvest
from tests.test_pls_search_harvest_gap_enqueue import _row, _runner

SLOT1 = "PLD,SCMR,PLC,CLD,PTD"


def test_gap_focus_defaults_off():
    assert Settings.model_fields["PLS_SEARCH_HARVEST_GAP_FOCUS"].default is False
CASE_URL = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2017S2202&court=&Row=0"


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_GAP_FOCUS", True)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", SLOT1)


def _query(**kwargs) -> PlsSearchHarvestQuery:
    body = dict(
        source_name="PakistanLawSite",
        status="pending",
        priority=100,
        gap_size=0,
        query_json={"reporter": "PLD", "year": 2017},
    )
    body.update(kwargs)
    return PlsSearchHarvestQuery(**body)


async def _pick(db):
    return await harvest.pick_next_query(db, slot=1)


@pytest.mark.asyncio
async def test_gap_focus_off_does_not_claim_done_rows_with_a_gap(db, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", SLOT1)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_GAP_FOCUS", False)
    done = _query(
        query_key="search:reporter=PLC|year=2017",
        query_json={"reporter": "PLC", "year": 2017},
        status="done",
        gap_size=40,
        priority=960,
        last_run_at=datetime.now(timezone.utc) - timedelta(hours=8),
    )
    open_row = _query(
        query_key="search:reporter=PTD|year=2019",
        query_json={"reporter": "PTD", "year": 2019},
        status="in_progress",
        gap_size=0,
        priority=107,
    )
    db.add(done)
    db.add(open_row)
    await db.flush()

    picked = await _pick(db)
    assert picked is not None
    assert picked.query_key == open_row.query_key
    assert done.status == "done"


@pytest.mark.asyncio
async def test_gap_focus_prefers_the_larger_cooled_gap(db, monkeypatch):
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    bigger = _query(
        query_key="search:reporter=PLC|year=2017",
        query_json={"reporter": "PLC", "year": 2017},
        status="done",
        gap_size=40,
        priority=960,
        last_run_at=now - timedelta(hours=8),
    )
    smaller = _query(
        query_key="search:reporter=SCMR|year=2017|keyword=b",
        query_json={"reporter": "SCMR", "year": 2017, "keyword": "b"},
        status="done",
        gap_size=2,
        priority=501,
        last_run_at=now - timedelta(hours=30),
    )
    gap0 = _query(
        query_key="search:reporter=PTD|year=2019",
        query_json={"reporter": "PTD", "year": 2019},
        status="in_progress",
        gap_size=0,
        priority=1,
        last_run_at=now - timedelta(days=2),
    )
    db.add(bigger)
    db.add(smaller)
    db.add(gap0)
    await db.flush()

    picked = await _pick(db)
    assert picked is not None
    assert picked.query_key == bigger.query_key


@pytest.mark.asyncio
async def test_gap_focus_skips_a_gap_that_ran_inside_the_cooldown(db, monkeypatch):
    """A full pass does not immediately repeat the largest gap."""
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    hot = _query(
        query_key="search:reporter=PLC|year=2017",
        query_json={"reporter": "PLC", "year": 2017},
        status="done",
        gap_size=40,
        last_run_at=now - timedelta(minutes=5),
    )
    cooled = _query(
        query_key="search:reporter=CLD|year=2026",
        query_json={"reporter": "CLD", "year": 2026},
        status="done",
        gap_size=1,
        last_run_at=now - timedelta(hours=8),
    )
    db.add(hot)
    db.add(cooled)
    await db.flush()

    picked = await _pick(db)
    assert picked is not None
    assert picked.query_key == cooled.query_key


@pytest.mark.asyncio
async def test_gap_focus_claims_keyword_shard_not_split_parent(db, monkeypatch):
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    parent = _query(
        query_key="search:reporter=PLD|year=2017",
        query_json={"reporter": "PLD", "year": 2017},
        status="split",
        gap_size=82,
        priority=918,
        last_run_at=now - timedelta(days=20),
        cursor_json={"page": 3, "row_index": 0},
        last_error="pagination ended without next link",
    )
    db.add(parent)
    await db.flush()
    shard = _query(
        query_key="search:reporter=PLD|year=2017|keyword=p",
        query_json={"reporter": "PLD", "year": 2017, "keyword": "p"},
        status="done",
        gap_size=12,
        priority=501,
        parent_id=parent.id,
        last_run_at=now - timedelta(hours=8),
        cursor_json={"page": 1, "row_index": 4, "linked_gap_attempts": 4},
        last_error="unresolved linked gap: 12 row(s) still unmatched after detail fetch",
        rows_seen=259,
        pages_enumerated=3,
    )
    exhausted = _query(
        query_key="search:reporter=PLD|year=2017|keyword=a",
        query_json={"reporter": "PLD", "year": 2017, "keyword": "a"},
        status="done",
        gap_size=0,
        parent_id=parent.id,
        last_run_at=now - timedelta(hours=8),
    )
    db.add(shard)
    db.add(exhausted)
    await db.flush()

    picked = await _pick(db)
    assert picked is not None
    assert picked.query_key == shard.query_key
    assert parent.status == "split"


@pytest.mark.asyncio
async def test_gap_focus_claims_split_row_when_it_has_no_shards(db, monkeypatch):
    _enable(monkeypatch)
    parent = _query(
        query_key="search:reporter=SCMR|year=2025",
        query_json={"reporter": "SCMR", "year": 2025},
        status="split",
        gap_size=263,
        last_run_at=datetime.now(timezone.utc) - timedelta(days=2),
        cursor_json={"page": 2, "row_index": 1},
    )
    db.add(parent)
    await db.flush()

    picked = await _pick(db)
    assert picked is not None
    assert picked.query_key == parent.query_key


@pytest.mark.asyncio
async def test_open_gap0_runs_after_the_gap_pass_then_yields_the_tick(db, monkeypatch):
    """Gap-0 rows are not starved, and they do not take every following tick."""
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    recent_gap = _query(
        query_key="search:reporter=PLC|year=2017",
        query_json={"reporter": "PLC", "year": 2017},
        status="done",
        gap_size=40,
        last_run_at=now - timedelta(minutes=10),
    )
    older = _query(
        query_key="search:reporter=PTD|year=2019",
        query_json={"reporter": "PTD", "year": 2019},
        status="in_progress",
        gap_size=0,
        priority=107,
        last_run_at=now - timedelta(hours=10),
    )
    newer = _query(
        query_key="search:reporter=PLC|year=2004",
        query_json={"reporter": "PLC", "year": 2004},
        status="pending",
        gap_size=0,
        priority=122,
        last_run_at=now - timedelta(hours=8),
    )
    db.add(recent_gap)
    db.add(older)
    db.add(newer)
    await db.flush()

    picked = await _pick(db)
    assert picked is not None
    assert picked.query_key == older.query_key

    older.last_run_at = now
    await db.flush()

    again = await _pick(db)
    assert again is None


@pytest.mark.asyncio
async def test_claim_of_done_gap_keeps_cursor_counts_and_last_error(db, monkeypatch):
    _enable(monkeypatch)
    cursor = {
        "page": 1,
        "row_index": 0,
        "linked_gap_attempts": 4,
        "unresolved_samples": [{"case_name": "2017S2202", "citation": "2017 PLC 65"}],
    }
    done = _query(
        query_key="search:reporter=PLC|year=2017",
        query_json={"reporter": "PLC", "year": 2017},
        status="done",
        gap_size=40,
        priority=960,
        pages_enumerated=3,
        rows_seen=120,
        rows_known=80,
        rows_new=40,
        last_error="unresolved linked gap: 40 row(s) still unmatched after detail fetch",
        last_run_at=datetime.now(timezone.utc) - timedelta(hours=8),
        cursor_json=cursor,
    )
    db.add(done)
    await db.commit()

    row, lock = await harvest.claim_next_query(db, 1, redis_client=None)
    try:
        assert row is not None
        assert row.query_key == done.query_key
        assert row.status == "in_progress"
        assert row.cursor_json == cursor
        assert row.last_error.startswith("unresolved linked gap")
        assert row.pages_enumerated == 3
        assert row.rows_seen == 120
        assert row.rows_known == 80
        assert row.rows_new == 40
        assert row.gap_size == 40
    finally:
        if lock is not None:
            await lock.release()


def test_fresh_cursor_keeps_unresolved_identity_and_annexure_stamps():
    kept = fresh_cursor_keeping_annexure_state(
        {
            "page": 4,
            "row_index": 2,
            "non_judgment_annexures": ["1990HC-A1"],
            "annexure_stub_attempts": {"1990HC-A1": 2},
            "gap_samples": [{"title": "Versus"}],
            "unresolved_samples": [
                {
                    "case_name": "2017S2202",
                    "citation": "2017 PLC 65",
                    "detail_url": CASE_URL,
                    "page": 1,
                    "row_index": 3,
                    "query_key": "search:reporter=PLC|year=2017",
                    "attempt": 4,
                }
            ],
        }
    )
    assert kept["page"] == 1 and kept["row_index"] == 0
    assert kept["non_judgment_annexures"] == ["1990HC-A1"]
    assert "gap_samples" not in kept
    assert kept["unresolved_samples"][0]["case_name"] == "2017S2202"
    assert kept["unresolved_samples"][0]["query_key"] == "search:reporter=PLC|year=2017"


def test_reset_lap_does_not_drop_unresolved_identity():
    cursor = {
        "page": 1,
        "row_index": 0,
        "gap_samples": [{"citation": "2017 PLC 65"}],
        "unresolved_samples": [{"case_name": "2017S2202", "citation": "2017 PLC 65"}],
    }
    reset_lap_gap_diagnostics(cursor)
    assert cursor["gap_samples"] == []
    assert cursor["unresolved_samples"][0]["case_name"] == "2017S2202"


def test_remember_unmatched_gap_row_records_identity_without_wiping_a_prior_result():
    from scraper.pls_search_harvest_core import remember_unmatched_gap_row

    cursor: dict = {}
    row = {"citation": "2017 PLC 65", "detail_url": CASE_URL, "title": "Versus"}
    remember_unmatched_gap_row(
        cursor,
        row,
        page=2,
        row_index=7,
        query_key="search:reporter=PLC|year=2017",
        attempt=1,
    )
    sample = cursor["unresolved_samples"][0]
    assert sample["case_name"] == "2017S2202"
    assert sample["citation"] == "2017 PLC 65"
    assert sample["detail_url"] == CASE_URL
    assert sample["page"] == 2
    assert sample["row_index"] == 7
    assert sample["query_key"] == "search:reporter=PLC|year=2017"
    assert sample["attempt"] == 1
    assert "result" not in sample

    remember_unmatched_gap_row(
        cursor,
        row,
        page=2,
        row_index=7,
        query_key="search:reporter=PLC|year=2017",
        attempt=2,
        result="fetch_error",
        failure="fetch_error: timeout",
    )
    assert len(cursor["unresolved_samples"]) == 1
    updated = cursor["unresolved_samples"][0]
    assert updated["result"] == "fetch_error"
    assert updated["case_name"] == "2017S2202"
    assert updated["attempt"] == 2
    assert updated["failure"] == "fetch_error: timeout"

    remember_unmatched_gap_row(
        cursor,
        row,
        page=3,
        row_index=0,
        query_key="search:reporter=PLC|year=2017",
        attempt=3,
    )
    kept = cursor["unresolved_samples"][0]
    assert kept["result"] == "fetch_error"
    assert kept["failure"] == "fetch_error: timeout"
    assert kept["page"] == 3
    assert kept["attempt"] == 3


def test_remember_unmatched_gap_row_holds_more_than_the_old_twenty_sample_cap():
    from scraper.pls_search_harvest_core import remember_unmatched_gap_row

    cursor: dict = {}
    for index in range(21):
        remember_unmatched_gap_row(
            cursor,
            {"citation": f"2017 PLC {index}", "detail_url": f"https://example.test/{index}"},
            page=1,
            row_index=index,
            query_key="search:reporter=PLC|year=2017",
            attempt=1,
        )
    assert len(cursor["unresolved_samples"]) == 21
    assert cursor["unresolved_samples"][20]["citation"] == "2017 PLC 20"


@pytest.mark.asyncio
async def test_unmatched_identity_is_committed_before_detail_fetch(db, monkeypatch):
    rows = [_row("2017 PLC 65", CASE_URL)]
    runner = _runner(monkeypatch, db, {"page1": (rows, None)}, known=set(), preserve="fetch_error")
    query = _query(
        query_key="search:reporter=PLC|year=2017",
        query_json={"reporter": "PLC", "year": 2017},
        status="in_progress",
        pages_enumerated=1,
        rows_seen=1,
        rows_known=0,
        rows_new=0,
        gap_size=1,
        cursor_json={"page": 1, "row_index": 0},
    )
    db.add(query)
    await db.commit()

    original = runner.pipeline.fetch_detail
    seen = {"n": 0}

    async def fetch_detail(url):
        if "CaseName=2017S2202" in url:
            seen["n"] += 1
            if seen["n"] == 1:
                async with SessionLocal() as other:
                    fresh = (
                        await other.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.id == query.id))
                    ).scalar_one()
                samples = (fresh.cursor_json or {}).get("unresolved_samples") or []
                assert samples, "unmatched identity must be committed before the detail fetch"
                assert samples[0]["case_name"] == "2017S2202"
                assert samples[0]["citation"] == "2017 PLC 65"
                assert samples[0]["page"] == 1
                assert samples[0]["row_index"] == 0
                assert samples[0]["query_key"] == query.query_key
                assert samples[0]["attempt"] == 1
        return await original(url)

    runner.pipeline.fetch_detail = fetch_detail
    first = await runner.run_query(query, {})
    assert first["status"] == "in_progress"
    stored = (query.cursor_json or {}).get("unresolved_samples") or []
    assert stored[0]["result"] == "fetch_error"
    assert stored[0]["case_name"] == "2017S2202"

    second = await runner.run_query(query, {})
    assert second["status"] == "done"
    assert (query.last_error or "").startswith("unresolved linked gap")
    survived = (query.cursor_json or {}).get("unresolved_samples") or []
    assert survived[0]["case_name"] == "2017S2202"
    assert survived[0]["citation"] == "2017 PLC 65"
    assert survived[0]["query_key"] == query.query_key
    assert survived[0]["attempt"] == 2
    assert seen["n"] >= 1
    await db.rollback()
