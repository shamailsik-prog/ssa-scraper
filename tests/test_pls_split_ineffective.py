"""Split children that repeat the parent row-set are marked split_ineffective."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import harvest_result_fingerprint_from_keys
from scraper.tasks import pls_search_harvest as harvest


def _rows(n: int):
    return [{"citation": f"2017 PLD {i}", "detail_url": f"https://example.test/{i}"} for i in range(1, n + 1)]


@pytest.mark.asyncio
async def test_duplicate_split_child_marks_ineffective_and_stops_siblings(db, monkeypatch):
    fp = harvest_result_fingerprint_from_keys({f"2017 PLD {i}" for i in range(1, 260)})
    parent_id = uuid4()
    parent = PlsSearchHarvestQuery(
        id=parent_id,
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017",
        query_json={"reporter": "PLD", "year": 2017},
        status="split",
        rows_seen=259,
        cursor_json={"split_source_fingerprint": fp},
    )
    child_a = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017|keyword=i",
        query_json={"reporter": "PLD", "year": 2017, "keyword": "i"},
        parent_id=parent_id,
        status="done",
        rows_seen=259,
        cursor_json={"result_fingerprint": fp},
    )
    child_b = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017|keyword=j",
        query_json={"reporter": "PLD", "year": 2017, "keyword": "j"},
        parent_id=parent_id,
        status="pending",
    )
    db.add_all([parent, child_a, child_b])
    await db.flush()

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db = db
    runner.probe_mode = False

    aborted = await runner._maybe_abort_ineffective_split_child(child_b, fp, 259)
    assert aborted is True
    assert child_b.status == "split_ineffective"
    assert child_b.last_error
    assert child_b.status == "split_ineffective"


@pytest.mark.asyncio
async def test_split_child_matching_parent_fingerprint_aborts(db):
    fp = "abc123"
    parent_id = uuid4()
    parent = PlsSearchHarvestQuery(
        id=parent_id,
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017",
        query_json={"reporter": "PLD", "year": 2017},
        status="split",
        rows_seen=259,
        cursor_json={"split_source_fingerprint": fp},
    )
    child = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017|keyword=k",
        query_json={"reporter": "PLD", "year": 2017, "keyword": "k"},
        parent_id=parent_id,
        status="in_progress",
        rows_seen=259,
    )
    db.add_all([parent, child])
    await db.flush()

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db = db
    assert await runner._maybe_abort_ineffective_split_child(child, fp, 259) is True
    assert child.status == "split_ineffective"
