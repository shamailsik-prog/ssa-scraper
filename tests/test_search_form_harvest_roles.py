"""Dashboard search-form harvest role aliases (party box → keyword splits)."""

import pytest

from scraper.config import settings
from scraper.extractors.deterministic import introspect_search_form
from scraper.pls_search_harvest_core import build_harvest_form_values, unmapped_harvest_reason
from scraper.search_form_roles import verify_harvest_role_samples
from scraper.tasks.search_map import build_map_record, verify_selectors
from tests.test_dashboard_citation_form_map import DASHBOARD_SNIPPET


def _dashboard_map():
    proposal = verify_selectors(DASHBOARD_SNIPPET, introspect_search_form(DASHBOARD_SNIPPET))
    record = build_map_record(proposal, DASHBOARD_SNIPPET, "test")
    record["surface"] = proposal.get("surface")
    return record


def test_party_field_aliased_to_keyword_for_harvest(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_KEYWORD_PARTY_ALIAS", True)
    smap = _dashboard_map()
    proof = verify_harvest_role_samples(smap)
    assert proof["court_mapped"] is True
    assert proof["keyword_mapped"] is True
    assert proof["ok"] is True
    q = {"reporter": "SCMR", "year": 2025, "keyword": "a"}
    assert unmapped_harvest_reason(smap, q, {"page": 1}) is None
    values = build_harvest_form_values(smap, q, {"page": 1})
    assert values.get("party") == "a" or values.get("keyword") == "a"
    assert values.get("reporter") == "SCMR"
    assert "court" not in values


def test_court_child_maps_to_court_role():
    smap = _dashboard_map()
    q = {"reporter": "SCMR", "year": 2025, "court": "SC"}
    assert unmapped_harvest_reason(smap, q, {"page": 1}) is None
    values = build_harvest_form_values(smap, q, {"page": 1})
    assert values.get("court") == "SC"
    assert "keyword" not in values
    assert "party" not in values


def test_plain_journal_year_query_fills_only_reporter_and_year():
    smap = _dashboard_map()
    # Production v203 shape: party control only, no persisted keyword role.
    smap["fields"] = {k: v for k, v in smap["fields"].items() if k != "keyword"}
    smap["limits"] = {**(smap.get("limits") or {}), "harvest_role_aliases": {"keyword": "party"}}
    q = {"reporter": "YLR", "year": 2024}
    values = build_harvest_form_values(smap, q, {"page": 1})
    assert set(values.keys()) == {"reporter", "year"}
    proof = verify_harvest_role_samples(smap)
    ylr = next(c for c in proof["samples"] if c["query"] == q)
    assert ylr["value_roles"] == ["reporter", "year"]


def test_keyword_split_prefers_party_when_stale_keyword_role_exists(monkeypatch):
    """Production maps may still list a keyword control that is not the live filter; splits must hit party."""
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_KEYWORD_PARTY_ALIAS", False)
    smap = _dashboard_map()
    smap["limits"] = {**(smap.get("limits") or {}), "harvest_role_aliases": {"keyword": "party"}}
    smap["fields"]["keyword"] = {"name": "stale", "selector": "#stale-keyword", "kind": "text", "options": []}
    q = {"reporter": "SCMR", "year": 2025, "keyword": "z"}
    values = build_harvest_form_values(smap, q, {"page": 1})
    assert values.get("party") == "z"
    assert "keyword" not in values


def test_keyword_split_applies_to_party_box_without_alias_flag(monkeypatch):
    """PLD 2017 letter splits must submit the keyword token even when KEYWORD_PARTY_ALIAS is off."""
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_KEYWORD_PARTY_ALIAS", False)
    smap = _dashboard_map()
    smap["fields"] = {k: v for k, v in smap["fields"].items() if k != "keyword"}
    q = {"reporter": "PLD", "year": 2017, "keyword": "i"}
    values = build_harvest_form_values(smap, q, {"page": 1})
    assert values.get("party") == "i"
    assert values.get("reporter") == "PLD"
    assert values.get("year") == "2017"


def test_v203_plain_and_court_queries_never_fill_party_without_keyword_flag(monkeypatch):
    """v203 dashboard map: journal-year and court children must not write the party box by default."""
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_KEYWORD_PARTY_ALIAS", False)
    smap = _dashboard_map()
    smap["fields"] = {k: v for k, v in smap["fields"].items() if k != "keyword"}
    for q in (
        {"reporter": "SCMR", "year": 2025},
        {"reporter": "SCMR", "year": 2025, "court": "SC"},
    ):
        values = build_harvest_form_values(smap, q, {"page": 1})
        assert "party" not in values
        assert "keyword" not in values
        assert values.get("reporter") == q["reporter"]
        assert values.get("year") == str(q["year"])
        if q.get("court"):
            assert values.get("court") == q["court"]
