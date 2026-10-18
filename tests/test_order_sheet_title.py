from scraper.parsers.order_sheet_title import case_title_from_order_sheet

HEADER = """1990 C L C 1205   [Peshawar]   Before Muhammad Bashir Khan Jehangiri, J
Mst. KAUSAR BIBI---Petitioner
versus
MUHAMMAD MUSHTAQ and 6 others---Respondents
Civil Revision No.329 of 1989, decided on 22nd November, 1989.
(a) Civil Procedure Code (V of 1908)--- S. 115 - revision."""

CITED_FIRST = """1999 CLC 1 [Karachi] referred.
Mst. ALI---Petitioner
versus
THE STATE---Respondent
Civil Appeal No. 1 of 2020, decided on 1st January, 2020."""


def test_order_sheet_title_from_header_parties():
    title = case_title_from_order_sheet(HEADER)
    assert title
    assert "KAUSAR BIBI" in title
    assert "MUSHTAQ" in title
    assert "1999 CLC" not in title


def test_order_sheet_title_ignores_cited_case_when_header_has_parties():
    title = case_title_from_order_sheet(CITED_FIRST)
    assert title and "ALI" in title and "STATE" in title
