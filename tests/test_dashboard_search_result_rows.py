"""Dashboard Citation Search loads AJAX results into #rightmenu, not #archivedpatientGrid."""

from scraper.extractors.deterministic import extract_result_rows_deterministic

DASHBOARD_AJAX_RESULTS = """
<html><body>
<div id="rightmenu">
  <table><thead><tr><th>Citation</th><th>Title</th><th>Court</th></tr></thead>
  <tbody>
    <tr><td>PLD 2026 SC 9</td><td><a href="/Login/ReferenceCaseLawSearch?CaseName=x">New v State</a></td><td>Supreme Court</td></tr>
  </tbody></table>
</div>
</body></html>
"""

DUAL_MAP = {
    "limits": {"dashboard_citation_fields": True, "surface": "query_form"},
    "result_layout": {
        "row_selector": "#archivedpatientGrid tbody tr",
        "dashboard_row_selector": "#rightmenu table tr",
        "columns": {"citation": 0, "title": 1, "court": 2},
        "detail_link_selector": "a[href]",
    },
}


def test_dashboard_ajax_results_use_rightmenu_not_archived_grid():
    out = extract_result_rows_deterministic(html=DASHBOARD_AJAX_RESULTS, search_map=DUAL_MAP, base_url="https://example.com")
    rows = out["result_rows"]
    assert len(rows) == 1
    assert rows[0]["citation"] == "PLD 2026 SC 9"
    assert rows[0]["detail_url"] and "ReferenceCaseLawSearch" in rows[0]["detail_url"]
    assert "rightmenu" in (out["field_evidence"].get("result_rows") or "")
