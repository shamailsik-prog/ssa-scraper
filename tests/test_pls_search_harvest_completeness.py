"""Search-harvest queue completeness must never imply missing judgments are verified."""

import pytest

from scraper.models import PlsSearchHarvestQuery
from scraper.tasks.pls_search_harvest import completeness_report


@pytest.mark.asyncio
async def test_unseeded_search_harvest_plan_is_not_complete(db):
    report = await completeness_report(db, known_sample=0)
    assert report["queue"].get("done", 0) == 0
    assert report["complete"] is False
    assert report["verification_scope"] == "search_harvest_queue_only"
    assert report["independent_source_verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,site_total,rows_seen,pages,expected_complete,expected_unverified",
    [
        ("done", 9, 3, 1, False, 1),     # Known site results never enumerated
        ("done", None, 3, 2, False, 1),  # Scheduling done; source total unknown
        ("done", 3, 3, 0, False, 1),     # No page evidence
        ("split", 500, 500, 2, False, 0),  # Split parent needs child verification
        ("skipped", None, 0, 0, False, 0),
        ("done", 3, 3, 1, True, 0),      # Queue-only, not independent certification
        ("done", 0, 0, 1, True, 0),      # Confirmed empty result with known total
    ],
)
async def test_completeness_fails_closed_on_unverified_rows(
    db, status, site_total, rows_seen, pages, expected_complete, expected_unverified
):
    db.add(
        PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key="completeness-single-query",
            query_json={"reporter": "CLC", "year": 2024},
            status=status,
            site_total_results=site_total,
            rows_seen=rows_seen,
            pages_enumerated=pages,
        )
    )
    await db.flush()
    report = await completeness_report(db, known_sample=0)
    assert report["complete"] is expected_complete
    assert report["unverified_done_queries"] == expected_unverified
    assert report["independent_source_verified"] is False
    assert report["known_citation_test"]["independent"] is False
    if status != "done":
        assert report["unfinished_statuses"][status] == 1
    await db.rollback()
