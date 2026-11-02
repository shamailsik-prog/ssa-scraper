"""Persistent PLS Error-700 annexure stubs become known non-judgments.

PLD|1990 gap 13: CaseNames such as 1990SC-A1 and 1990HC-A1..A11 are title
"Versus" with a null citation, a null pdf, and no keys. Detail fetch returns
pls_error_code_700. A real AJK judgment uses a longer suffix (1990HC-A2001)
and carries a citation, so it must stay on the ordinary linked-gap path.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import func, select

from scraper.models import Judgment, PlsSearchHarvestQuery, ScraperStaging
from scraper.pls_search_harvest_core import (
    ANNEXURE_SHORT_TEXT_REASON,
    classify_persistent_annexure_stub,
    fresh_cursor_keeping_annexure_state,
    is_annexure_stub_row,
    mapped_annexure_names,
)
from scraper.tasks import pls_search_harvest as harvest
from tests.test_pls_search_harvest_gap_enqueue import _runner


def _detail_url(case_name: str) -> str:
    return (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        f"?CaseName={case_name}&court=&Row=0&bookName=undefined"
    )


def _annexure_row(case_name: str = "1990HC-A1", **overrides) -> dict:
    row = {
        "citation": None,
        "title": "Versus",
        "pdf_url": None,
        "detail_url": _detail_url(case_name),
    }
    row.update(overrides)
    return row


def _signal_700(detail, route, row):
    route["pls_detail_failure"] = "fetch_error: pls_error_code_700"
    return "fetch_error"


def _short_text(detail, route, row):
    route["pls_detail_failure"] = ANNEXURE_SHORT_TEXT_REASON
    return ANNEXURE_SHORT_TEXT_REASON


def test_annexure_case_name_is_one_or_two_digits_only():
    assert is_annexure_stub_row(_annexure_row("1990HC-A1")) == "1990HC-A1"
    assert is_annexure_stub_row(_annexure_row("1990HC-A11")) == "1990HC-A11"
    assert is_annexure_stub_row(_annexure_row("1990SC-A2")) == "1990SC-A2"
    assert is_annexure_stub_row(_annexure_row("1990hc-a7")) == "1990HC-A7"
    assert is_annexure_stub_row(_annexure_row("1990HC-A100")) is None
    assert is_annexure_stub_row(_annexure_row("1990HC-A2001")) is None
    assert is_annexure_stub_row(_annexure_row("1990AT-A1")) is None
    assert is_annexure_stub_row(_annexure_row("1990HCA-A1")) is None


def test_annexure_stub_requires_versus_title_and_empty_citation_pdf_and_keys():
    assert is_annexure_stub_row(_annexure_row(title=" Versus ")) == "1990HC-A1"
    assert is_annexure_stub_row(_annexure_row(title="versus")) is None
    assert is_annexure_stub_row(_annexure_row(title="Appellant Versus Respondent")) is None
    assert is_annexure_stub_row(_annexure_row(citation="1990 PLC (CS) 733")) is None
    assert is_annexure_stub_row(_annexure_row(pdf_url="https://example.test/a.pdf")) is None
    assert is_annexure_stub_row(_annexure_row("1990HC-A2001", citation="1990 PLC (CS) 733")) is None


def test_one_error_700_does_not_map_and_two_do():
    cursor: dict = {}
    row = _annexure_row()
    first = classify_persistent_annexure_stub(
        cursor, row, result="fetch_error", failure="fetch_error: pls_error_code_700", slot_active=True
    )
    assert first is None
    assert cursor["annexure_stub_attempts"]["1990HC-A1"] == 1
    second = classify_persistent_annexure_stub(
        cursor, row, result="fetch_error", failure="fetch_error: pls_error_code_700", slot_active=True
    )
    assert second == "non_judgment_annexure"
    assert "1990HC-A1" in mapped_annexure_names(cursor)


def test_short_text_counts_as_the_same_annexure_signal():
    cursor: dict = {}
    row = _annexure_row("1990SC-A2")
    assert (
        classify_persistent_annexure_stub(
            cursor, row, result=ANNEXURE_SHORT_TEXT_REASON, failure=None, slot_active=True
        )
        is None
    )
    mapped = classify_persistent_annexure_stub(
        cursor,
        row,
        result="fetch_error",
        failure="fetch_error: pls_error_code_700",
        slot_active=True,
    )
    assert mapped == "non_judgment_annexure"


def test_bare_fetch_error_and_inactive_slot_do_not_count():
    cursor: dict = {}
    row = _annexure_row()
    assert classify_persistent_annexure_stub(cursor, row, result="fetch_error", failure=None, slot_active=True) is None
    assert "annexure_stub_attempts" not in cursor
    assert (
        classify_persistent_annexure_stub(
            cursor, row, result="fetch_error", failure="fetch_error: pls_error_code_700", slot_active=False
        )
        is None
    )
    assert "annexure_stub_attempts" not in cursor
    assert (
        classify_persistent_annexure_stub(
            cursor, row, result="fetch_error", failure="fetch_error: pls_error_page", slot_active=True
        )
        is None
    )
    assert "annexure_stub_attempts" not in cursor


def test_ajk_case_with_citation_is_not_mapped_after_repeated_700():
    cursor: dict = {}
    row = _annexure_row("1990HC-A2001", citation="1990 PLC (CS) 733", title="Versus")
    for _ in range(2):
        assert (
            classify_persistent_annexure_stub(
                cursor, row, result="fetch_error", failure="fetch_error: pls_error_code_700", slot_active=True
            )
            is None
        )
    assert mapped_annexure_names(cursor) == set()
    assert "annexure_stub_attempts" not in cursor


def test_legacy_fetch_error_sample_plus_one_new_700_maps():
    """Samples stored before the failure reason was kept say result=fetch_error.

    One of those, plus a new Error 700, is the second linked-gap attempt.
    A sample that names a different failure does not count.
    """
    row = _annexure_row()
    legacy = {"unresolved_samples": [{"result": "fetch_error", "citation": None, "detail_url": row["detail_url"]}]}
    mapped = classify_persistent_annexure_stub(
        legacy, row, result="fetch_error", failure="fetch_error: pls_error_code_700", slot_active=True
    )
    assert mapped == "non_judgment_annexure"

    other = {
        "unresolved_samples": [
            {
                "result": "fetch_error",
                "failure": "fetch_error: pls_error_page",
                "detail_url": row["detail_url"],
            }
        ]
    }
    assert (
        classify_persistent_annexure_stub(
            other, row, result="fetch_error", failure="fetch_error: pls_error_code_700", slot_active=True
        )
        is None
    )
    assert other["annexure_stub_attempts"]["1990HC-A1"] == 1


def test_query_level_attempt_counter_alone_does_not_map():
    cursor = {"linked_gap_attempts": 3}
    row = _annexure_row()
    assert (
        classify_persistent_annexure_stub(
            cursor, row, result="fetch_error", failure="fetch_error: pls_error_code_700", slot_active=True
        )
        is None
    )


def test_reopen_cursor_keeps_mapped_annexure_names():
    kept = fresh_cursor_keeping_annexure_state(
        {
            "page": 4,
            "row_index": 2,
            "non_judgment_annexures": ["1990HC-A1", "1990SC-A2"],
            "annexure_stub_attempts": {"1990HC-A11": 1},
            "gap_samples": [{"title": "Versus"}],
        }
    )
    assert kept["page"] == 1 and kept["row_index"] == 0
    assert kept["non_judgment_annexures"] == ["1990HC-A1", "1990SC-A2"]
    assert kept["annexure_stub_attempts"] == {"1990HC-A11": 1}
    assert "gap_samples" not in kept


def _gap_query() -> PlsSearchHarvestQuery:
    return PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=1990",
        query_json={"reporter": "PLD", "year": 1990},
        status="in_progress",
        pages_enumerated=1,
        rows_seen=1,
        rows_known=0,
        rows_new=0,
        gap_size=1,
        cursor_json={"page": 1, "row_index": 0},
    )


async def _corpus_counts(db) -> tuple[int, int]:
    judgments = int((await db.execute(select(func.count()).select_from(Judgment))).scalar() or 0)
    staging = int((await db.execute(select(func.count()).select_from(ScraperStaging))).scalar() or 0)
    return judgments, staging


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve", [_signal_700, _short_text])
async def test_matching_annexure_maps_on_second_attempt_and_closes_gap(db, monkeypatch, caplog, preserve):
    caplog.set_level(logging.INFO)
    before = await _corpus_counts(db)
    row = _annexure_row("1990HC-A1")
    runner = _runner(monkeypatch, db, {"page1": ([row], None)}, known=set(), preserve=preserve)
    runner.slot_is_active = True
    query = _gap_query()
    db.add(query)
    await db.flush()

    first = await runner.run_query(query, {})
    assert first["gap_size"] == 1
    assert first["staged_this_tick"] == 0
    assert first["status"] == "in_progress"
    first_samples = (query.cursor_json or {}).get("unresolved_samples") or []
    assert first_samples and first_samples[0]["result"] != "non_judgment_annexure"
    assert "non_judgment_annexures" not in (query.cursor_json or {})

    second = await runner.run_query(query, {})
    assert second["gap_size"] == 0
    assert second["staged_this_tick"] == 0
    assert second["rows_known"] == 1
    assert second["status"] == "done"
    assert not (query.last_error or "").startswith("unresolved linked gap")
    samples = (query.cursor_json or {}).get("unresolved_samples") or []
    assert len(samples) == 1
    assert samples[0]["result"] == "non_judgment_annexure"
    assert samples[0]["case_name"] == "1990HC-A1"
    mapping_logs = [record for record in caplog.records if "mapped non_judgment_annexure" in record.message]
    assert len(mapping_logs) == 1
    assert "1990HC-A1" in mapping_logs[0].message
    assert await _corpus_counts(db) == before

    fetched = len(runner.fetched)
    third = await runner.run_query(query, {})
    assert third["gap_size"] == 0
    assert third["staged_this_tick"] == 0
    assert len(runner.fetched) == fetched
    await db.rollback()


@pytest.mark.asyncio
async def test_ajk_citation_row_stays_on_the_two_try_after_repeated_700(db, monkeypatch):
    row = _annexure_row("1990HC-A2001", citation="1990 PLC (CS) 733", title="Versus")
    runner = _runner(monkeypatch, db, {"page1": ([row], None)}, known=set(), preserve=_signal_700)
    runner.slot_is_active = True
    query = _gap_query()
    db.add(query)
    await db.flush()

    await runner.run_query(query, {})
    second = await runner.run_query(query, {})
    assert second["gap_size"] == 1
    assert second["staged_this_tick"] == 0
    assert (query.last_error or "").startswith("unresolved linked gap")
    samples = (query.cursor_json or {}).get("unresolved_samples") or []
    assert samples
    assert all(sample["result"] != "non_judgment_annexure" for sample in samples)
    assert "non_judgment_annexures" not in (query.cursor_json or {})
    await db.rollback()


@pytest.mark.asyncio
async def test_annexure_without_700_or_short_text_stays_on_the_two_try(db, monkeypatch):
    row = _annexure_row()
    runner = _runner(monkeypatch, db, {"page1": ([row], None)}, known=set(), preserve="fetch_error")
    runner.slot_is_active = True
    query = _gap_query()
    db.add(query)
    await db.flush()

    await runner.run_query(query, {})
    second = await runner.run_query(query, {})
    assert second["gap_size"] == 1
    assert (query.last_error or "").startswith("unresolved linked gap")
    samples = (query.cursor_json or {}).get("unresolved_samples") or []
    assert samples and all(sample["result"] == "fetch_error" for sample in samples)
    await db.rollback()


@pytest.mark.asyncio
async def test_legacy_unresolved_sample_closes_annexure_gap_on_the_next_700(db, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    row = _annexure_row("1990HC-A11")
    runner = _runner(monkeypatch, db, {"page1": ([row], None)}, known=set(), preserve=_signal_700)
    runner.slot_is_active = True
    query = _gap_query()
    query.cursor_json = {
        "page": 1,
        "row_index": 0,
        "linked_gap_attempts": 3,
        "unresolved_samples": [
            {"result": "fetch_error", "citation": None, "detail_url": row["detail_url"]},
            {"result": "fetch_error", "citation": None, "detail_url": row["detail_url"]},
        ],
    }
    db.add(query)
    await db.flush()

    result = await runner.run_query(query, {})
    assert result["gap_size"] == 0
    assert result["staged_this_tick"] == 0
    assert result["status"] == "done"
    samples = (query.cursor_json or {}).get("unresolved_samples") or []
    assert any(sample.get("result") == "non_judgment_annexure" and sample.get("case_name") == "1990HC-A11" for sample in samples)
    assert len([record for record in caplog.records if "mapped non_judgment_annexure" in record.message]) == 1
    await db.rollback()


@pytest.mark.asyncio
async def test_annexure_map_leaves_a_normal_fetch_error_unresolved(db, monkeypatch):
    """Only the annexure stub leaves the gap. A normal Error 700 stays on the two-try."""
    annexure = _annexure_row("1990SC-A1")
    other = {
        "citation": "1990 PLD 1",
        "title": "Khan Versus Ali",
        "pdf_url": None,
        "detail_url": "https://example.test/real",
    }
    runner = _runner(
        monkeypatch,
        db,
        {"page1": ([annexure, other], None)},
        known=set(),
        preserve=_signal_700,
    )
    runner.slot_is_active = True
    query = _gap_query()
    query.rows_seen = 2
    query.gap_size = 2
    db.add(query)
    await db.flush()

    await runner.run_query(query, {})
    second = await runner.run_query(query, {})
    assert second["gap_size"] == 1
    assert second["rows_known"] == 1
    assert second["staged_this_tick"] == 0
    assert (query.last_error or "").startswith("unresolved linked gap")
    samples = (query.cursor_json or {}).get("unresolved_samples") or []
    by_url = {sample["detail_url"]: sample["result"] for sample in samples}
    assert by_url[annexure["detail_url"]] == "non_judgment_annexure"
    assert by_url[other["detail_url"]] == "fetch_error"
    await db.rollback()


@pytest.mark.asyncio
async def test_inactive_slot_does_not_map_an_annexure_stub(db, monkeypatch):
    row = _annexure_row()
    runner = _runner(monkeypatch, db, {"page1": ([row], None)}, known=set(), preserve=_signal_700)
    query = _gap_query()
    db.add(query)
    await db.flush()

    await runner.run_query(query, {})
    second = await runner.run_query(query, {})
    assert second["gap_size"] == 1
    assert "non_judgment_annexures" not in (query.cursor_json or {})
    samples = (query.cursor_json or {}).get("unresolved_samples") or []
    assert all(sample["result"] != "non_judgment_annexure" for sample in samples)
    await db.rollback()


@pytest.mark.asyncio
async def test_reporter_year_reopen_keeps_mapped_annexure_names(db):
    query = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=1990",
        query_json={"reporter": "PLD", "year": 1990},
        status="done",
        pages_enumerated=1,
        rows_seen=1,
        rows_known=1,
        gap_size=0,
        cursor_json={
            "page": 1,
            "row_index": 0,
            "non_judgment_annexures": ["1990HC-A1"],
            "annexure_stub_attempts": {"1990HC-A1": 2},
        },
    )
    db.add(query)
    await db.flush()

    reopened = await harvest.reopen_finished_reporter_year_queries(db, limit=4)
    assert reopened == 1
    assert query.cursor_json["page"] == 1
    assert query.cursor_json["non_judgment_annexures"] == ["1990HC-A1"]
    assert query.cursor_json["annexure_stub_attempts"]["1990HC-A1"] == 2
    await db.rollback()
