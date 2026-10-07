"""Unit tests for PakistanLawSite search-driven gap harvest planner, splitter, and accounting."""

from __future__ import annotations

from scraper.pls_search_harvest_core import (
    build_harvest_form_values,
    compute_gap_size,
    iter_base_plan_queries,
    make_query_key,
    normalize_query_json,
    partition_rows_by_known,
    parse_total_results_from_html,
    split_oversized_query,
)
from scraper.tasks.search_map import build_map_record, verify_selectors
from scraper.extractors.deterministic import introspect_search_form
from tests.fixtures import citation_search_hybrid_html, results_html


def test_make_query_key_stable_and_ordered():
    q = {"year": 2020, "reporter": "PLD", "court": "SC"}
    assert make_query_key(q) == make_query_key({"reporter": "PLD", "year": 2020, "court": "SC"})
    assert make_query_key(q).startswith("search:reporter=PLD|")


def test_iter_base_plan_queries_reporter_year_grid():
    queries = list(iter_base_plan_queries(["PLD", "SCMR"], [2024, 2023]))
    keys = {make_query_key(q) for q in queries}
    assert len(keys) == 4
    assert normalize_query_json({"reporter": "PLD", "year": 2024}) in queries


def test_split_oversized_by_court_then_keyword():
    parent = {"reporter": "PLD", "year": 2020}
    courts = split_oversized_query(parent, 800, result_cap=500, court_options=["SC", "HC"])
    assert len(courts) == 2
    assert courts[0]["court"] == "SC"
    with_court = {"reporter": "PLD", "year": 2020, "court": "SC"}
    keywords = split_oversized_query(with_court, 800, result_cap=500, court_options=["SC"])
    assert len(keywords) == 26
    assert keywords[0]["keyword"] == "a"


def test_partition_rows_and_gap_accounting():
    rows = [
        {"citation": "PLD 2024 SC 1", "detail_url": "/1"},
        {"citation": "PLD 2024 SC 2", "detail_url": "/2"},
    ]
    known, new_rows = partition_rows_by_known(rows, {"PLD 2024 SC 1"})
    assert known == 1
    assert len(new_rows) == 1
    assert compute_gap_size(100, 40, 55) == 60
    assert compute_gap_size(None, 40, 55) == 15


def test_parse_total_results_from_fixture_html():
    html = results_html(
        [("PLD 2024 SC 1", "A", "Supreme Court", "/case/1")],
        next_page=None,
    ).replace("</body>", "<p>1,234 results found</p></body>")
    assert parse_total_results_from_html(html) == 1234


def test_dashboard_search_map_detected_for_harvest_skip():
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline

    dashboard = {
        "fields": {
            "reporter": {"selector": "#Citation_Category_Search_dropdown"},
            "year": {"selector": "#Citation_Year_Search_input"},
        }
    }
    assert PakistanLawSitePipeline._is_dashboard_citation_fields(dashboard)
    assert not PakistanLawSitePipeline._is_dashboard_citation_fields({"fields": {"page": {"selector": "#x"}}})


def test_build_harvest_form_values_from_introspected_map():
    html = citation_search_hybrid_html()
    proposal = verify_selectors(html, introspect_search_form(html))
    search_map = build_map_record(proposal, html, "deterministic")
    search_map["surface"] = proposal.get("surface")
    values = build_harvest_form_values(
        search_map,
        {"reporter": "PLD", "year": 2024, "keyword": "contract"},
        {"page": 2},
    )
    assert values.get("reporter") == "PLD"
    assert values.get("year") == "2024"
    assert values.get("keyword") == "contract"
