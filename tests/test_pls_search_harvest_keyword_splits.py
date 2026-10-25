"""Keyword split children must submit the letter filter and see smaller result sets than the parent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scraper.extractors.deterministic import extract_result_rows_deterministic, introspect_search_form
from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import build_harvest_form_values
from scraper.tasks import pls_search_harvest as harvest
from scraper.tasks.search_map import build_map_record, verify_selectors
from tests.pls_harvest_filter_proof import FIXTURE_DIR, load_fixture_html, load_manifest
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


def test_fixture_manifest_present():
    manifest = load_manifest()
    assert manifest["reporter"] == "SCMR" and manifest["year"] == 2025
    assert (FIXTURE_DIR / "parent.html").is_file()
    for key in ("child_keyword_a", "child_keyword_z"):
        assert key in manifest["queries"]


def test_fixture_rows_extract_from_captured_html():
    manifest = load_manifest()
    smap = _fixture_search_map()
    counts = {}
    citation_sets = {}
    for key, meta in manifest["queries"].items():
        html = load_fixture_html(meta["html_file"])
        out = extract_result_rows_deterministic(html=html, search_map=smap, base_url="https://www.pakistanlawsite.com")
        rows = out["result_rows"]
        counts[key] = len(rows)
        citation_sets[key] = {r.get("citation") for r in rows if r.get("citation")}
        assert counts[key] == meta["row_count"]
    parent_n = counts["parent"]
    child_a = counts["child_keyword_a"]
    child_z = counts["child_keyword_z"]
    assert child_a < parent_n and child_z < parent_n
    assert child_a != child_z
    assert citation_sets["child_keyword_a"] != citation_sets["child_keyword_z"]


def test_keyword_child_form_values_differ_from_parent():
    smap = _dashboard_map()
    parent = build_harvest_form_values(smap, {"reporter": "SCMR", "year": 2025}, {"page": 1})
    child_a = build_harvest_form_values(smap, {"reporter": "SCMR", "year": 2025, "keyword": "a"}, {"page": 1})
    child_z = build_harvest_form_values(smap, {"reporter": "SCMR", "year": 2025, "keyword": "z"}, {"page": 1})
    assert parent.get("party") is None and "keyword" not in parent
    assert child_a.get("party") == "a" and child_z.get("party") == "z"
    assert child_a != child_z


@pytest.mark.asyncio
async def test_keyword_children_return_smaller_row_sets_than_parent(db, monkeypatch):
    manifest = load_manifest()
    smap = _fixture_search_map()
    html_by_key = {
        key: load_fixture_html(meta["html_file"]) for key, meta in manifest["queries"].items()
    }
    captured: list[dict] = []

    async def fetch_results(search_map, values):
        captured.append(dict(values))
        token = (values.get("party") or values.get("keyword") or "").strip()
        if token == "a":
            html = html_by_key["child_keyword_a"]
        elif token == "z":
            html = html_by_key["child_keyword_z"]
        else:
            html = html_by_key["parent"]
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
    monkeypatch.setattr(
        harvest,
        "unmapped_harvest_reason",
        lambda sm, qj, cur: None,
    )

    async def extract_result_rows(self, *, html, search_map, base_url, content_hash=None):
        out = extract_result_rows_deterministic(html=html, search_map=search_map, base_url=base_url)
        return SimpleNamespace(data=out)

    monkeypatch.setattr(
        "scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows",
        extract_result_rows,
    )
    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.__init__", lambda self, *a, **k: None)

    parent_n = manifest["queries"]["parent"]["row_count"]
    child_n = manifest["queries"]["child_keyword_a"]["row_count"]

    parent_q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=SCMR|year=2025",
        query_json={"reporter": "SCMR", "year": 2025},
        status="in_progress",
    )
    child_q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=SCMR|year=2025|keyword=a",
        query_json={"reporter": "SCMR", "year": 2025, "keyword": "a"},
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
    assert captured[1].get("party") == "a"
    await db.rollback()
