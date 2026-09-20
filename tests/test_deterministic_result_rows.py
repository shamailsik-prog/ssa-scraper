from scraper.extractors.deterministic import extract_result_rows_deterministic


def test_extract_result_rows_skips_login_check_and_uses_real_detail_link():
    html = """
    <html><body>
      <table id="archivedpatientGrid">
        <thead><tr><th>#</th><th>Citation</th><th>Title</th><th>Court</th><th>Read</th></tr></thead>
        <tbody>
          <tr>
            <td>1</td>
            <td>PLD 2024 SC 249</td>
            <td>Gamma versus State</td>
            <td>Supreme Court</td>
            <td>
              <a href="/login/check?ReturnUrl=%2FLogin%2FCitationSearch">Read</a>
              <a href="/Login/ReferenceCaseLawSearch?CaseName=2006K249&court=&Row=0&bookName=undefined">Judgment</a>
            </td>
          </tr>
        </tbody>
      </table>
    </body></html>
    """
    search_map = {
        "result_layout": {
            "row_selector": "#archivedpatientGrid tbody tr",
            "columns": {"citation": 1, "title": 2, "court": 3},
            "detail_link_selector": "a[href]",
        }
    }
    out = extract_result_rows_deterministic(
        html=html,
        search_map=search_map,
        base_url="https://www.pakistanlawsite.com/Login/CitationSearch",
    )
    row = out["result_rows"][0]
    assert row["detail_url"] == (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=2006K249&court=&Row=0&bookName=undefined"
    )
    assert "/login/check" not in (row["detail_url"] or "").lower()
