"""Keyword split children must submit the letter filter and see smaller result sets than the parent."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from scraper.extractors.deterministic import extract_result_rows_deterministic, introspect_search_form
from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import build_harvest_form_values
from scraper.tasks import pls_search_harvest as harvest
from scraper.tasks.search_map import build_map_record, verify_selectors
from tests.pls_harvest_filter_proof import FIXTURE_DIR, HTML_FILES, load_fixture_html, load_totals
from tests.test_dashboard_citation_form_map import DASHBOARD_SNIPPET
from tests.test_pls_search_harvest_pagination import _runner_factory


def _dashboard_map():
    snippet = DASHBOARD_SNIPPET.replace(
        '<input type="text" id="Citation_Court_Search_input" placeholder="Enter Court"/>',
        '<input type="text" id="Citation_Court_Search_input" placeholder="Enter Court"/>'
        '<input type="text" id="Citation_Party_Search_input" placeholder="Enter Party"/>',
    )
    proposal = verify_selectors(snippet, introspect_search_form(snippet))
    record = build_map_record(proposal, snippet, "test")
    record["surface"] = proposal.get("surface")
    limits = dict(record.get("limits") or {})
    limits["harvest_role_aliases"] = {"keyword": "party"}
    record["limits"] = limits
    return record


def _fixture_search_map():
    smap = _dashboard_map()
    smap["limits"] = {**(smap.get("limits") or {}), "dashboard_citation_fields": True, "surface": "query_form"}
    smap["result_layout"] = {
        "row_selector": "#archivedpatientGrid tbody tr",
        "dashboard_row_selector": "#rightmenu table tr",
        "columns": {"citation": 0, "title": 1, "court": 2},
        "detail_link_selector": "a[href]",
    }
    return smap


def test_live_capture_fixture_bundle_present():
    totals = load_totals()
    assert totals["tip"] == "beb7076"
    assert totals["slot"] == 1
    for key in ("parent", "child_keyword_m", "child_keyword_z", "child_keyword_a_low_selectivity"):
        assert (FIXTURE_DIR / HTML_FILES[key]).is_file()


def test_fixture_rows_match_live_capture_totals():
    totals = load_totals()
    smap = _fixture_search_map()
    expected = {
        "parent": totals["parent"]["rows"],
        "child_keyword_m": totals["m"]["rows"],
        "child_keyword_z": totals["z"]["rows"],
    }
    citation_sets = {}
    for key, exp in expected.items():
        html = load_fixture_html(key)
        rows = extract_result_rows_deterministic(
            html=html, search_map=smap, base_url="https://www.pakistanlawsite.com"
        )["result_rows"]
        assert len(rows) == exp
        citation_sets[key] = {r.get("citation") for r in rows if r.get("citation")}
    parent_n, m_n, z_n = expected["parent"], expected["child_keyword_m"], expected["child_keyword_z"]
    assert parent_n > m_n > z_n
    assert citation_sets["child_keyword_m"] != citation_sets["child_keyword_z"]


def test_keyword_a_is_low_selectivity_identical_to_parent_capture():
    """party=`a` returned the same 264-row page as parent (contains-match, not a useful split)."""
    totals = load_totals()
    assert totals["a"]["identical_to_parent"] is True
    assert totals["a"]["rows"] == totals["parent"]["rows"]
    parent_path = FIXTURE_DIR / HTML_FILES["parent"]
    a_path = FIXTURE_DIR / HTML_FILES["child_keyword_a_low_selectivity"]
    assert hashlib.md5(parent_path.read_bytes()).hexdigest() == totals["parent"]["md5"]
    assert hashlib.md5(a_path.read_bytes()).hexdigest() == totals["a"]["md5"]
    assert parent_path.read_bytes() == a_path.read_bytes()


def test_keyword_child_form_values_differ_from_parent():
    smap = _dashboard_map()
    parent = build_harvest_form_values(smap, {"reporter": "SCMR", "year": 2025}, {"page": 1})
    child_m = build_harvest_form_values(smap, {"reporter": "SCMR", "year": 2025, "keyword": "m"}, {"page": 1})
    child_z = build_harvest_form_values(smap, {"reporter": "SCMR", "year": 2025, "keyword": "z"}, {"page": 1})
    assert parent.get("party") is None and "keyword" not in parent
    assert child_m.get("party") == "m" and child_z.get("party") == "z"
    assert child_m != child_z


@pytest.mark.asyncio
async def test_keyword_children_return_smaller_row_sets_than_parent(db, monkeypatch):
    totals = load_totals()
    smap = _fixture_search_map()
    html_by_party = {
        "": load_fixture_html("parent"),
        "m": load_fixture_html("child_keyword_m"),
        "z": load_fixture_html("child_keyword_z"),
    }
    captured: list[dict] = []

    async def fetch_results(search_map, values):
        captured.append(dict(values))
        token = (values.get("party") or values.get("keyword") or "").strip()
        html = html_by_party.get(token, html_by_party[""])
        return SimpleNamespace(html=html, url="https://www.pakistanlawsite.com/Login/Check")

    pages = {"unused": ([], None)}
    runner = _runner_factory(monkeypatch, db, pages, total=None)
    runner.dry_run = True
    runner.pipeline.fetch_results = fetch_results
    monkeypatch.setattr(
        harvest,
        "build_harvest_form_values",
        lambda sm, qj, cur: build_harvest_form_values(sm, qj, cur),
    )
    monkeypatch.setattr(harvest, "unmapped_harvest_reason", lambda sm, qj, cur: None)

    async def extract_result_rows(self, *, html, search_map, base_url, content_hash=None):
        out = extract_result_rows_deterministic(html=html, search_map=search_map, base_url=base_url)
        return SimpleNamespace(data=out)

    monkeypatch.setattr(
        "scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows",
        extract_result_rows,
    )
    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.__init__", lambda self, *a, **k: None)

    parent_n = totals["parent"]["rows"]
    child_n = totals["m"]["rows"]

    parent_q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=SCMR|year=2025",
        query_json={"reporter": "SCMR", "year": 2025},
        status="in_progress",
    )
    child_q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=SCMR|year=2025|keyword=m",
        query_json={"reporter": "SCMR", "year": 2025, "keyword": "m"},
        status="in_progress",
    )
    db.add(parent_q)
    db.add(child_q)
    await db.flush()

    parent_out = await runner.run_query(parent_q, smap)
    child_out = await runner.run_query(child_q, smap)

    assert parent_out["rows_seen"] == parent_n
    assert child_out["rows_seen"] == child_n
    assert child_out["rows_seen"] < parent_out["rows_seen"]
    assert captured[0].get("party") is None
    assert captured[1].get("party") == "m"
    await db.rollback()
