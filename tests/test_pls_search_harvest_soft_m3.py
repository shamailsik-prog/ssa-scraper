"""Soft M3: multi-page linked gaps must leave durable unmatched evidence.

PLC|2017 on tip 83416f82 (2026-11-05, GAP_FOCUS live): pages_enumerated=3,
rows 120/80/40, gap 40, linked_gap_attempts=12, unresolved_samples NULL,
last_error "unresolved linked gap: 40 row(s) still unmatched after detail fetch".
Soft M2 stamps CaseName samples and closes phantoms only for a one-page
confirming reread. A multi-page reread kept the stored gap and wrote nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scraper.models import PlsSearchHarvestQuery
from scraper.tasks import pls_search_harvest as harvest
from tests.test_pls_search_harvest_gap_enqueue import _row, _runner


def _case_row(citation: str, case_name: str) -> dict:
    return {
        "citation": citation,
        "title": "Versus",
        "detail_url": (
            "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
            f"?CaseName={case_name}&court=&Row=0"
        ),
    }


def _paged_runner(monkeypatch, db, pages, *, known=None, preserve="fetch_error"):
    """Like the gap-enqueue runner, but a next-page URL returns that page's grid."""
    runner = _runner(monkeypatch, db, pages, known=known, preserve=preserve)
    fetched = runner.fetched

    async def fetch_detail(url):
        fetched.append(url)
        if url in pages:
            return SimpleNamespace(html=url, url=url)
        return SimpleNamespace(html="", url=url)

    runner.pipeline.fetch_detail = fetch_detail
    return runner


@pytest.mark.asyncio
async def test_multi_page_confirming_reread_stamps_evidence_when_no_row_is_observed(db, monkeypatch):
    """Page 1 of a 3-page ledger is all known and has no next link.

    Stored linked unseen stays. Soft M2 must not shrink it. The reread still
    has to stamp unresolved_samples so the gap is not NULL forever.
    """
    page1 = [
        _row("2017 PLC 1", "https://example.test/1"),
        _row("2017 PLC 2", "https://example.test/2"),
    ]
    runner = _runner(
        monkeypatch,
        db,
        {"page1": (page1, None)},
        known={"2017 PLC 1", "2017 PLC 2"},
    )
    query = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLC|year=2017",
        query_json={"reporter": "PLC", "year": 2017},
        status="in_progress",
        pages_enumerated=3,
        rows_seen=6,
        rows_known=4,
        rows_new=2,
        gap_size=2,
        site_total_results=6,
        cursor_json={"page": 1, "row_index": 0, "linked_gap_attempts": 4},
    )
    db.add(query)
    await db.flush()

    result = await runner.run_query(query, {})

    assert result["rows_seen"] == 6
    assert result["rows_known"] == 4
    assert result["gap_size"] == 2
    assert query.site_total_results == 6
    assert query.pages_enumerated == 3
    assert not (query.cursor_json or {}).get("count_only_gap")
    assert (query.last_error or "").startswith("unresolved linked gap")
    samples = (query.cursor_json or {}).get("unresolved_samples") or []
    assert samples, "multi-page linked gap left unresolved_samples NULL"
    marker = samples[0]
    assert marker.get("result") == "multi_page_unmatched"
    assert marker.get("reason") == "confirming_reread_did_not_observe_row_identity"
    assert marker.get("query_key") == query.query_key
    assert marker.get("linked_unseen") == 2
    assert marker.get("pages_enumerated") == 3
    assert marker.get("page") == 1
    assert not marker.get("detail_url")
    assert not marker.get("case_name")
    await db.rollback()


def test_stamp_multi_page_linked_gap_does_not_touch_single_page_or_real_samples():
    """Soft M2 still owns the one-page path. Real CaseName samples stay as written."""
    from scraper.pls_search_harvest_core import stamp_multi_page_linked_gap

    single: dict = {}
    assert (
        stamp_multi_page_linked_gap(
            single,
            pages_enumerated=1,
            linked_unseen=2,
            query_key="search:reporter=SCMR|year=2017|keyword=b",
            attempt=5,
            rows_seen=4,
            rows_known=2,
            page=1,
        )
        is False
    )
    assert "unresolved_samples" not in single

    kept = [{"case_name": "2017S4", "page": 2, "detail_url": "https://example.test/4", "result": "fetch_error"}]
    multi = {"unresolved_samples": list(kept), "count_only_gap": {"reason": "leave-me"}}
    assert (
        stamp_multi_page_linked_gap(
            multi,
            pages_enumerated=3,
            linked_unseen=2,
            query_key="search:reporter=PLC|year=2017",
            attempt=5,
            rows_seen=6,
            rows_known=4,
            page=3,
        )
        is False
    )
    assert multi["unresolved_samples"] == kept
    assert multi["count_only_gap"]["reason"] == "leave-me"

    closed = {"unresolved_samples": [{"result": "multi_page_unmatched", "linked_unseen": 2}]}
    assert (
        stamp_multi_page_linked_gap(
            closed,
            pages_enumerated=3,
            linked_unseen=0,
            query_key="search:reporter=PLC|year=2017",
            attempt=6,
            rows_seen=6,
            rows_known=6,
            page=1,
        )
        is False
    )
    assert "unresolved_samples" not in closed


@pytest.mark.asyncio
async def test_multi_page_confirming_reread_keeps_case_names_from_later_pages(db, monkeypatch):
    """Unmatched rows on pages 2 and 3 are the evidence, not a count-only marker."""
    pages = {
        "page1": (
            [_case_row("2017 PLC 1", "2017S1"), _case_row("2017 PLC 2", "2017S2")],
            "page2",
        ),
        "page2": (
            [_case_row("2017 PLC 3", "2017S3"), _case_row("2017 PLC 4", "2017S4")],
            "page3",
        ),
        "page3": (
            [_case_row("2017 PLC 5", "2017S5"), _case_row("2017 PLC 6", "2017S6")],
            None,
        ),
    }
    known = {"2017 PLC 1", "2017 PLC 2", "2017 PLC 3", "2017 PLC 5"}
    runner = _paged_runner(monkeypatch, db, pages, known=known, preserve="fetch_error")
    query = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLC|year=2017",
        query_json={"reporter": "PLC", "year": 2017},
        status="in_progress",
        pages_enumerated=3,
        rows_seen=6,
        rows_known=4,
        rows_new=2,
        gap_size=2,
        site_total_results=6,
        cursor_json={"page": 1, "row_index": 0, "linked_gap_attempts": 4},
    )
    db.add(query)
    await db.flush()

    result = await runner.run_query(query, {})

    assert result["gap_size"] > 0
    assert result["rows_known"] < result["rows_seen"]
    assert not (query.cursor_json or {}).get("count_only_gap")
    samples = (query.cursor_json or {}).get("unresolved_samples") or []
    by_case = {sample.get("case_name"): sample for sample in samples}
    assert by_case["2017S4"]["page"] == 2
    assert by_case["2017S4"]["result"] == "fetch_error"
    assert by_case["2017S4"]["query_key"] == query.query_key
    assert by_case["2017S6"]["page"] == 3
    assert by_case["2017S6"]["citation"] == "2017 PLC 6"
    assert all(sample.get("case_name") for sample in samples)
    await db.rollback()
