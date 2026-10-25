"""Keyword split children must submit the letter filter and see smaller result sets than the parent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scraper.extractors.deterministic import introspect_search_form
from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import build_harvest_form_values
from scraper.tasks import pls_search_harvest as harvest
from scraper.tasks.search_map import build_map_record, verify_selectors
from tests.test_dashboard_citation_form_map import DASHBOARD_SNIPPET
from tests.test_pls_search_harvest_pagination import _row, _runner_factory


def _dashboard_map():
    snippet = DASHBOARD_SNIPPET.replace(
        '<input type="text" id="Citation_Court_Search_input" placeholder="Enter Court"/>',
        '<input type="text" id="Citation_Court_Search_input" placeholder="Enter Court"/>'
        '<input type="text" id="Citation_Party_Search_input" placeholder="Enter Party"/>',
    )
    proposal = verify_selectors(snippet, introspect_search_form(snippet))
    record = build_map_record(proposal, snippet, "test")
    record["surface"] = proposal.get("surface")
    return record


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
    smap = _dashboard_map()
    parent_rows = [_row(i) for i in range(1, 51)]
    child_rows = {
        "a": [_row("a1"), _row("a2")],
        "z": [_row("z1")],
    }
    captured: list[dict] = []

    pages = {"page1": (parent_rows, None)}
    runner = _runner_factory(monkeypatch, db, pages, total=None)
    runner.dry_run = True

    async def fetch_results(search_map, values):
        captured.append(dict(values))
        return SimpleNamespace(html="page1", url="page1")

    runner.pipeline.fetch_results = fetch_results
    monkeypatch.setattr(harvest, "build_harvest_form_values", lambda sm, qj, cur: build_harvest_form_values(sm, qj, cur))

    async def extract_result_rows(self, *, html, search_map, base_url):
        rows, nxt = pages[html]
        if captured:
            last = captured[-1]
            token = (last.get("party") or last.get("keyword") or "").strip()
            if token in child_rows:
                rows = child_rows[token]
        return SimpleNamespace(data={"result_rows": rows, "next_page": nxt})

    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows", extract_result_rows)

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

    assert parent_out["rows_seen"] == 50
    assert child_out["rows_seen"] == 2
    assert child_out["rows_seen"] < parent_out["rows_seen"]
    assert captured[0].get("party") is None
    assert captured[1].get("party") == "a"
    await db.rollback()
