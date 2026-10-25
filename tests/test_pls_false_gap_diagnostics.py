"""PLD-style false gaps: site rows without detail links."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import (
    GAP_SAMPLES_LIMIT,
    append_gap_sample,
    compute_gap_size,
    row_gap_diagnostics,
    search_harvest_may_mark_done,
    search_harvest_may_mark_done_unlinked,
    unlinked_gap_credit,
)
from scraper.tasks import pls_search_harvest as harvest
from tests.test_pls_search_harvest_pagination import _row, _runner_factory


def _linkless(n: int) -> dict:
    return {
        "citation": f"PLD 2018 SC {n}",
        "title": f"Title {n}",
        "detail_url": None,
        "pdf_url": None,
    }


def test_row_gap_diagnostics_and_sample_cap():
    row = {"citation": "PLD 2018 SC 1", "title": "T", "detail_url": None, "pdf_url": None}
    diag = row_gap_diagnostics(row)
    assert diag["citation"] == "PLD 2018 SC 1" and "PLD 2018 SC 1" in diag["keys"]

    cursor: dict = {}
    for i in range(GAP_SAMPLES_LIMIT + 5):
        append_gap_sample(cursor, {**row, "citation": f"PLD {i}"})
    assert len(cursor["gap_samples"]) == GAP_SAMPLES_LIMIT


def test_gap_size_credits_unlinked_only_within_seen_unknown():
    assert unlinked_gap_credit(90, 70, 10) == 10
    assert unlinked_gap_credit(90, 70, 30) == 20
    assert compute_gap_size(100, 70, 90, rows_unlinked=10) == 20
    assert compute_gap_size(100, 70, 90, rows_unlinked=30) == 10


def test_may_mark_done_ignores_unlinked():
    blocked, _ = search_harvest_may_mark_done(site_total=100, rows_seen=90, rows_on_page=5)
    assert blocked is False
    assert search_harvest_may_mark_done(site_total=100, rows_seen=100, rows_on_page=5)[0] is True


def _unlinked_ok(**kwargs):
    return search_harvest_may_mark_done_unlinked(pagination_confirmed=True, **kwargs)[0]


def test_done_unlinked_safety_returns_reason():
    ok, reason = search_harvest_may_mark_done_unlinked(
        site_total=100,
        rows_seen=100,
        rows_known=94,
        rows_unlinked=6,
        pagination_confirmed=True,
    )
    assert ok is False
    assert "exceeds 5%" in reason


@pytest.mark.asyncio
async def test_done_unlinked_safety_logged_and_stored(db, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    rows = [_linkless(i) for i in range(6)]
    pages = {"page1": (rows, None)}
    runner = _runner_factory(monkeypatch, db, pages, total=100)
    runner.dry_run = True
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:safety-cap",
        query_json={"reporter": "PLD", "year": 2018},
        status="in_progress",
        pages_enumerated=1,
        rows_seen=94,
        rows_known=94,
    )
    db.add(q)
    await db.flush()
    await runner.run_query(q, {})
    assert q.status == "in_progress"
    assert q.last_error and "exceeds 5%" in q.last_error
    assert any("safety-cap" in r.message and "exceeds 5%" in r.message for r in caplog.records)
    await db.rollback()


def test_done_unlinked_probe_cases():
    assert not _unlinked_ok(site_total=200, rows_seen=195, rows_known=190, rows_unlinked=5)
    assert not _unlinked_ok(site_total=300, rows_seen=290, rows_known=280, rows_unlinked=10)
    assert not _unlinked_ok(site_total=500, rows_seen=480, rows_known=460, rows_unlinked=20)
    assert _unlinked_ok(site_total=300, rows_seen=300, rows_known=290, rows_unlinked=10)
    assert _unlinked_ok(site_total=None, rows_seen=290, rows_known=280, rows_unlinked=10)
    assert not search_harvest_may_mark_done_unlinked(
        site_total=300,
        rows_seen=300,
        rows_known=290,
        rows_unlinked=10,
        pagination_confirmed=False,
    )[0]


@pytest.mark.asyncio
async def test_page1_reread_three_laps_resets_unlinked_each_tick(db, monkeypatch):
    """Re-read ticks keep pages_enumerated; lap reset must not accumulate linkless rows across laps."""
    counts = [10, 7, 4]
    call = {"n": 0}

    pages = {"page1": ([], None)}
    runner = _runner_factory(monkeypatch, db, pages, total=100)
    runner.dry_run = True

    async def extract_varying(self, *, html, search_map, base_url):
        n = counts[min(call["n"], len(counts) - 1)]
        call["n"] += 1
        rows = [_linkless(i) for i in range(n)]
        return SimpleNamespace(
            data={"result_rows": rows, "next_page": None, "total_results_if_shown": 100},
        )

    monkeypatch.setattr(
        "scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows",
        extract_varying,
    )

    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2018",
        query_json={"reporter": "PLD", "year": 2018},
        status="in_progress",
        pages_enumerated=1,
        rows_seen=10,
        rows_known=0,
        cursor_json={"page": 1, "row_index": 0},
    )
    db.add(q)
    await db.flush()

    for expected in counts:
        q.cursor_json = {"page": 1, "row_index": 0}
        await runner.run_query(q, {})
        assert q.cursor_json.get("rows_unlinked") == expected
    await db.rollback()


@pytest.mark.asyncio
async def test_page1_rerun_three_times_keeps_ten_unlinked(db, monkeypatch):
    rows = [_linkless(i) for i in range(10)]
    pages = {"page1": (rows, None)}
    runner = _runner_factory(monkeypatch, db, pages, total=100)
    runner.dry_run = True

    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2018",
        query_json={"reporter": "PLD", "year": 2018},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    for _ in range(3):
        q.pages_enumerated = 0
        q.rows_seen = 0
        q.rows_known = 0
        q.cursor_json = {"page": 1, "row_index": 0}
        await runner.run_query(q, {})
        assert q.cursor_json.get("rows_unlinked") == 10
        assert len(q.cursor_json.get("gap_samples") or []) == 10
    await db.rollback()


@pytest.mark.asyncio
async def test_early_stop_with_ten_linkless_not_done(db, monkeypatch):
    pages = {"page1": ([_linkless(i) for i in range(10)], None)}
    runner = _runner_factory(monkeypatch, db, pages, total=100)
    runner.dry_run = True
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:early-linkless",
        query_json={"reporter": "PLD", "year": 2018},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    await runner.run_query(q, {})
    assert q.status == "in_progress"
    assert q.cursor_json.get("rows_unlinked") == 10
    assert q.rows_seen == 10
    await db.rollback()


@pytest.mark.asyncio
async def test_timeout_does_not_double_count_unlinked(db, monkeypatch):
    linkless = [_linkless(i) for i in range(5)]
    linked = [_row(i) for i in range(1, 8)]
    pages = {"page1": (linked + linkless, None)}
    runner = _runner_factory(monkeypatch, db, pages, total=20)
    runner.dry_run = False
    monkeypatch.setattr(harvest.settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 1)

    clock = [0.0]

    def tick():
        clock[0] += 2.0
        return clock[0]

    monkeypatch.setattr(harvest.time, "monotonic", tick)

    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:timeout-unlinked",
        query_json={"reporter": "PLD", "year": 2018},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    await runner.run_query(q, {})
    first_unlinked = q.cursor_json.get("rows_unlinked") or 0
    assert first_unlinked == 0
    assert q.status == "in_progress"

    monkeypatch.setattr(harvest.settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)
    clock[0] = 0.0
    await runner.run_query(q, {})
    assert q.cursor_json.get("rows_unlinked") == 5

    q.pages_enumerated = 0
    q.rows_seen = 0
    q.cursor_json = {"page": 1, "row_index": 0}
    runner.dry_run = True
    await runner.run_query(q, {})
    assert q.cursor_json.get("rows_unlinked") == 5
    await db.rollback()


@pytest.mark.asyncio
async def test_known_citation_linkless_not_counted(db, monkeypatch):
    unlinked = _linkless(99)
    pages = {"page1": ([unlinked], None)}
    runner = _runner_factory(monkeypatch, db, pages, total=1)
    runner.dry_run = True

    async def known_keys(db, keys):
        return set(keys)

    monkeypatch.setattr(harvest, "load_known_citation_keys", known_keys)

    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:known-linkless",
        query_json={"reporter": "PLD", "year": 2018},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    await runner.run_query(q, {})
    assert q.cursor_json.get("rows_unlinked") in (None, 0)
    assert (q.cursor_json.get("rows_unlinked") or 0) == 0
    await db.rollback()


@pytest.mark.asyncio
async def test_run_query_logs_unmatched_and_done_unlinked(db, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO)
    unlinked = _linkless(99)
    linked = _row(1)
    pages = {"page1": ([linked, unlinked], None)}
    runner = _runner_factory(monkeypatch, db, pages, total=2)
    runner.dry_run = False

    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2018",
        query_json={"reporter": "PLD", "year": 2018},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    await runner.run_query(q, {})

    assert q.cursor_json.get("rows_unlinked") == 1
    samples = q.cursor_json.get("gap_samples") or []
    assert len(samples) == 2
    assert any(s["citation"] == "PLD 2018 SC 99" for s in samples)
    assert any("unmatched row" in r.message for r in caplog.records)
    assert q.gap_size == 1
    assert q.status == "done"
    assert q.rows_new == 1
    await db.rollback()


@pytest.mark.asyncio
async def test_pagination_gap_splits_parent_when_site_total_unreached(db, monkeypatch):
    """Pagination ended early with a positive gap: narrow via split instead of eternal in_progress stall."""
    from sqlalchemy import func, select

    pages = {"page1": ([_row(i) for i in range(1, 9)], None)}
    runner = _runner_factory(monkeypatch, db, pages, total=100)
    runner.dry_run = True
    search_map = {
        "fields": {
            "reporter": {"options": ["PLD"]},
            "court": {"options": ["SC", "HC"]},
            "keyword": {},
        }
    }
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2018",
        query_json={"reporter": "PLD", "year": 2018},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    await runner.run_query(q, search_map)

    assert q.status == "split"
    assert "pagination_gap" in (q.split_reason or "")
    child_count = (
        await db.execute(
            select(func.count())
            .select_from(PlsSearchHarvestQuery)
            .where(PlsSearchHarvestQuery.parent_id == q.id)
        )
    ).scalar()
    assert child_count == 2
    await db.rollback()
