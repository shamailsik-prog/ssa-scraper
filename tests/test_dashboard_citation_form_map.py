"""Dashboard Citation Search panel (formless) must map to query_form with reporter×year."""
from scraper.extractors.deterministic import introspect_search_form

DASHBOARD_SNIPPET = """
<html><head><title>Pakistan Law Site</title></head><body>
<a href="/Login/LogOff">Log Off</a>
<div class="citationSearchDiv">
  <select id="Citation_Category_Search_dropdown">
    <option value="PLD">PLD</option>
    <option value="SCMR">SCMR</option>
    <option value="CLC">CLC</option>
  </select>
  <input type="text" id="Citation_Year_Search_input" placeholder="Enter Year (required)" value="2026"/>
  <input type="text" id="Citation_Court_Search_input" placeholder="Enter Court"/>
  <input type="text" id="Citation_Code_Or_Page_Search_input" placeholder="Enter Code or Page"/>
  <button class="btn btn-success Citation_Search_btn" type="button">Search</button>
</div>
<table id="whatsNewTable"><tr><th>x</th></tr></table>
</body></html>
"""


def test_dashboard_citation_panel_is_query_form():
    probe = introspect_search_form(DASHBOARD_SNIPPET)
    assert probe["surface"] == "query_form"
    roles = {f["role"] for f in probe["fields"]}
    assert "reporter" in roles
    assert "year" in roles
    assert "submit" in roles
    reporter = next(f for f in probe["fields"] if f["role"] == "reporter")
    assert reporter["selector"] == "#Citation_Category_Search_dropdown"
    assert "PLD" in reporter["options"]


def test_grid_only_still_grid_surface():
    html = """<html><body><table id="archivedpatientGrid"><tr><th>Citation</th></tr></table></body></html>"""
    probe = introspect_search_form(html)
    assert probe["surface"] == "grid_surface_no_query_form"
    assert probe["fields"] == []
