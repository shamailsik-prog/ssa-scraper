"""Split ineffectiveness: duplicate court tokens, parent halt, and post-children union."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import (
    harvest_result_fingerprint_from_keys,
    split_children_case_coverage_incomplete,
    split_children_union_incomplete,
)
from scraper.search_form_harvest_vocabulary import _options_from_field, capture_harvest_split_vocabulary
from scraper.tasks import pls_search_harvest as harvest


def test_options_from_field_value_only_case_insensitive_dedupe():
    fields = {
        "court": {
            "options": ["LHC"],
            "option_labels": [["LHC", "Lahore High Court"], ["lhc", "lahore high court"]],
        }
    }
    assert _options_from_field(fields, "court") == ["LHC"]


def test_capture_vocabulary_does_not_duplicate_label_as_court():
    html = ""
    fields = {"court": {"options": [], "option_labels": [["LHC", "Lahore High Court"]]}}
    vocab = capture_harvest_split_vocabulary(html, fields)
    assert vocab["courts"] == ["LHC"]


def test_split_children_union_incomplete():
    keys_a = {f"k{i}" for i in range(80)}
    keys_b = {f"k{i}" for i in range(40, 100)}
    assert split_children_union_incomplete(120, [keys_a, keys_b]) is True
    keys_full = {f"k{i}" for i in range(120)}
    assert split_children_union_incomplete(120, [keys_full]) is False


@pytest.mark.asyncio
async def test_two_duplicate_children_do_not_halt_wave(db):
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
        query_key="search:reporter=PLD|year=2017|court=LHC",
        query_json={"reporter": "PLD", "year": 2017, "court": "LHC"},
        parent_id=parent_id,
        status="done",
        rows_seen=259,
        cursor_json={"result_fingerprint": fp},
    )
    child_b = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017|court=Lahore High Court",
        query_json={"reporter": "PLD", "year": 2017, "court": "Lahore High Court"},
        parent_id=parent_id,
        status="pending",
        rows_seen=259,
    )
    db.add_all([parent, child_a, child_b])
    await db.flush()

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db = db
    runner.probe_mode = False

    aborted = await runner._maybe_abort_ineffective_split_child(child_b, fp, 259, set())
    assert aborted is False
    assert child_b.status == "pending"


@pytest.mark.asyncio
async def test_three_matching_children_halt_and_parent_needs_resplit(db):
    fp = harvest_result_fingerprint_from_keys({f"2017 PLD {i}" for i in range(1, 260)})
    parent_id = uuid4()
    parent = PlsSearchHarvestQuery(
        id=parent_id,
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017",
        query_json={"reporter": "PLD", "year": 2017},
        status="split",
        rows_seen=259,
        cursor_json={"split_source_fingerprint": fp, "split_expected_rows_seen": 259},
    )
    children = []
    for label, st in (("a", "done"), ("b", "done"), ("c", "in_progress")):
        children.append(
            PlsSearchHarvestQuery(
                source_name="PakistanLawSite",
                query_key=f"search:reporter=PLD|year=2017|keyword={label}",
                query_json={"reporter": "PLD", "year": 2017, "keyword": label},
                parent_id=parent_id,
                status=st,
                rows_seen=259,
                cursor_json={"result_fingerprint": fp} if st == "done" else {},
            )
        )
    pending = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2017|keyword=d",
        query_json={"reporter": "PLD", "year": 2017, "keyword": "d"},
        parent_id=parent_id,
        status="pending",
    )
    db.add_all([parent, *children, pending])
    await db.flush()

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db = db
    current = children[2]
    aborted = await runner._maybe_abort_ineffective_split_child(current, fp, 259, set())
    assert aborted is True
    assert current.status == "split_ineffective"
    assert pending.status == "split_ineffective"
    await db.refresh(parent)
    assert parent.status == "needs_resplit"
    assert "rows_seen=259" in (parent.last_error or "")


@pytest.mark.asyncio
async def test_enqueue_split_children_idempotent(db):
    parent_id = uuid4()
    parent = PlsSearchHarvestQuery(
        id=parent_id,
        source_name="PakistanLawSite",
        query_key="search:parent",
        query_json={"reporter": "PLD", "year": 2020},
        status="split",
    )
    existing_child = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2020|court=SC",
        query_json={"reporter": "PLD", "year": 2020, "court": "SC"},
        parent_id=parent_id,
        status="pending",
    )
    db.add_all([parent, existing_child])
    await db.flush()

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db = db
    await runner._enqueue_split_children(
        parent,
        [
            {"reporter": "PLD", "year": 2020, "court": "SC"},
            {"reporter": "PLD", "year": 2020, "court": "HC"},
        ],
        site_total=800,
        cap=500,
    )
    await db.flush()
    rows = (
        await db.execute(
            select(PlsSearchHarvestQuery).where(
                PlsSearchHarvestQuery.source_name == "PakistanLawSite",
                PlsSearchHarvestQuery.parent_id == parent_id,
            )
        )
    ).scalars().all()
    assert len(rows) == 2


def test_split_aliases_cannot_fake_parent_row_coverage():
    # One judgment contributes several aliases but cannot cover two parent rows.
    child_keys = [{"case:2024SC1", "2024 SCMR 1", "SCMR 2024 1"}]
    assert split_children_union_incomplete(2, child_keys) is False  # original unsafe proxy
    assert split_children_case_coverage_incomplete(2, child_keys) is True
    assert split_children_case_coverage_incomplete(
        2, [{"case:2024SC1"}, {"case:2024SC2"}]
    ) is False
    # Citation-only rows cannot be proven complete from flat aliases.
    assert split_children_case_coverage_incomplete(1, [{"2024 SCMR 1"}]) is True


@pytest.mark.asyncio
async def test_split_parent_flags_alias_inflated_coverage(db):
    parent_id = uuid4()
    parent = PlsSearchHarvestQuery(
        id=parent_id,
        source_name="PakistanLawSite",
        query_key="search:reporter=SCMR|year=2024",
        query_json={"reporter": "SCMR", "year": 2024},
        status="split",
        rows_seen=2,
        cursor_json={"split_expected_rows_seen": 2},
    )
    child = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=SCMR|year=2024|court=SC",
        query_json={"reporter": "SCMR", "year": 2024, "court": "SC"},
        parent_id=parent_id,
        status="done",
        rows_seen=1,
        cursor_json={"result_keys": ["case:2024SC1", "2024 SCMR 1", "SCMR 2024 1"]},
    )
    db.add_all([parent, child])
    await db.flush()
    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db = db
    await runner._maybe_finalize_split_parent(parent_id)
    assert parent.status == "needs_resplit"
    assert "1 distinct CaseNames" in (parent.last_error or "")
    await db.rollback()
