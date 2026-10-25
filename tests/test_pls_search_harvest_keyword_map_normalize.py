"""Dashboard maps missing the party control must still submit keyword split letters."""

from __future__ import annotations

from scraper.pls_search_harvest_core import (
    build_harvest_form_values,
    keyword_child_redundant_with_parent,
    split_oversized_query,
)
from scraper.search_form_roles import keyword_splits_expressible, normalize_harvest_search_map


def _minimal_dashboard_map():
    return {
        "fields": {
            "reporter": {"selector": "#Citation_Category_Search_dropdown", "kind": "select"},
            "year": {"selector": "#Citation_Year_Search_input", "kind": "text"},
        },
        "limits": {"dashboard_citation_fields": True},
    }


def test_normalize_injects_party_control_for_dashboard_map():
    smap = normalize_harvest_search_map(_minimal_dashboard_map())
    assert "party" in smap["fields"]
    assert smap["fields"]["party"]["selector"] == "#Citation_Party_Search_input"
    assert smap["limits"]["harvest_role_aliases"]["keyword"] == "party"
    assert keyword_splits_expressible(smap)


def test_keyword_child_values_use_party_on_minimal_dashboard_map():
    smap = _minimal_dashboard_map()
    values = build_harvest_form_values(
        smap,
        {"reporter": "SCMR", "year": 2025, "keyword": "m"},
        {"page": 1},
    )
    assert values.get("party") == "m"
    assert "keyword" not in values


def test_legacy_party_box_mapped_as_keyword_role_is_migrated():
    smap = {
        "fields": {
            "reporter": {"selector": "#Citation_Category_Search_dropdown"},
            "year": {"selector": "#Citation_Year_Search_input"},
            "keyword": {"selector": "#Citation_Party_Search_input", "kind": "text"},
        },
        "limits": {"dashboard_citation_fields": True},
    }
    normalized = normalize_harvest_search_map(smap)
    assert "party" in normalized["fields"]
    assert "keyword" not in normalized["fields"]
    values = build_harvest_form_values(
        normalized,
        {"reporter": "SCMR", "year": 2025, "keyword": "z"},
        {"page": 1},
    )
    assert values.get("party") == "z"


def test_split_skips_keyword_when_party_control_not_expressible():
    smap = {"fields": {"reporter": {"selector": "#r"}, "year": {"selector": "#y"}}}
    kids = split_oversized_query(
        {"reporter": "PLD", "year": 2020, "court": "SC"},
        900,
        result_cap=500,
        court_options=["SC"],
        search_map=smap,
    )
    assert kids == []


def test_keyword_child_redundant_detector():
    assert keyword_child_redundant_with_parent(
        parent_site_total=264,
        parent_rows_seen=264,
        child_site_total=264,
        child_rows_seen=264,
    )
    assert not keyword_child_redundant_with_parent(
        parent_site_total=264,
        parent_rows_seen=264,
        child_site_total=None,
        child_rows_seen=210,
    )
