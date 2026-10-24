"""Harvest split tokens must come from the live search map, not guessed DB or a–z lists."""

from __future__ import annotations

from scraper.pls_search_harvest_core import split_oversized_query
from scraper.search_form_harvest_vocabulary import (
    capture_harvest_split_vocabulary,
    court_split_values,
    keyword_split_values,
)

ROLES = {"reporter", "year", "month", "court", "bench", "party_initial", "keyword"}


def test_capture_court_tokens_from_dashboard_script():
    html = """
    <script>var courtList = ["SC", "LHC", "PHC"];</script>
    """
    fields = {"court": {"options": []}}
    vocab = capture_harvest_split_vocabulary(html, fields)
    assert vocab["courts"] == ["SC", "LHC", "PHC"]


def test_court_split_values_prefers_limits_vocabulary():
    smap = {
        "limits": {"harvest_split_vocabulary": {"courts": ["FSC", "SHC"], "keywords": []}},
        "fields": {"court": {"options": ["ignored"]}},
    }
    assert court_split_values(smap) == ["FSC", "SHC"]


def test_keyword_split_empty_without_dashboard_vocab():
    smap = {"limits": {"harvest_split_vocabulary": {"courts": [], "keywords": []}}, "fields": {}}
    assert keyword_split_values(smap) == []


def test_keyword_split_uses_party_options_when_keyword_empty():
    smap = {
        "limits": {"harvest_split_vocabulary": {"courts": [], "keywords": []}},
        "fields": {"party": {"options": ["Bank", "State"]}},
    }
    assert keyword_split_values(smap) == ["Bank", "State"]


def test_split_skips_keyword_without_vocab_or_legacy(monkeypatch):
    from scraper.config import settings

    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_LEGACY_ALPHA_KEYWORD_SPLITS", False)
    q = {"reporter": "PLD", "year": 2020, "month": 1, "court": "SC", "bench": "single", "party_initial": "a"}
    kids = split_oversized_query(
        q,
        900,
        result_cap=500,
        court_options=["SC"],
        keyword_splits=[],
        available_roles=ROLES,
    )
    assert kids == []


def test_split_uses_dashboard_keyword_vocab():
    q = {"reporter": "PLD", "year": 2020, "month": 1, "court": "SC", "bench": "single", "party_initial": "a"}
    kids = split_oversized_query(
        q,
        900,
        result_cap=500,
        court_options=["SC"],
        keyword_splits=["bail", "tax"],
        available_roles=ROLES,
    )
    assert [c["keyword"] for c in kids] == ["bail", "tax"]
