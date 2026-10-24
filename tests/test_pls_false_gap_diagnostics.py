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
)
from scraper.tasks import pls_search_harvest as harvest
from tests.test_pls_search_harvest_pagination import _row, _runner_factory


def test_row_gap_diagnostics_and_sample_cap():
    row = {"citation": "PLD 2018 SC 1", "title": "T", "detail_url": None, "pdf_url": None}
    diag = row_gap_diagnostics(row)
    assert diag["citation"] == "PLD 2018 SC 1" and "PLD 2018 SC 1" in diag["keys"]

    cursor: dict = {}
    for i in range(GAP_SAMPLES_LIMIT + 5):
        append_gap_sample(cursor, {**row, "citation": f"PLD {i}"})
    assert len(cursor["gap_samples"]) == GAP_SAMPLES_LIMIT


def test_gap_size_and_may_mark_done_exclude_unlinked():
    assert compute_gap_size(100, 70, 90, rows_unlinked=10) == 20
    assert compute_gap_size(100, 70, 90, rows_unlinked=30) == 0

    done, reason = search_harvest_may_mark_done(
        site_total=100,
        rows_seen=90,
        rows_on_page=5,
        rows_unlinked=10,
    )
    assert done is True and "no detail_url" in reason

    blocked, _ = search_harvest_may_mark_done(
        site_total=100,
        rows_seen=90,
        rows_on_page=5,
        rows_unlinked=5,
    )
    assert blocked is False


@pytest.mark.asyncio
async def test_run_query_logs_unmatched_and_counts_unlinked(db, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO)
    unlinked = {
        "citation": "PLD 2018 SC 99",
        "title": "No link row",
        "detail_url": None,
        "pdf_url": None,
    }
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
