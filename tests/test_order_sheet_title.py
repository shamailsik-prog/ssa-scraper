from scraper.parsers.order_sheet_title import case_title_from_order_sheet, is_valid_order_sheet_case_title

HEADER = """1990 C L C 1205   [Peshawar]   Before Muhammad Bashir Khan Jehangiri, J
Mst. KAUSAR BIBI---Petitioner
versus
MUHAMMAD MUSHTAQ and 6 others---Respondents
Civil Revision No.329 of 1989, decided on 22nd November, 1989.
(a) Civil Procedure Code (V of 1908)--- S. 115 - revision."""

EXPECTED_HEADER_TITLE = "Mst. KAUSAR BIBI versus MUHAMMAD MUSHTAQ and 6 others"

HEADNOTE_TRAP = """1990 PLD SC 5   Before Hon. Chief Justice, J
Bashir Ahmad versus Crown
Mst. KAUSAR BIBI---Petitioner
versus
MUHAMMAD MUSHTAQ---Respondents
Civil Appeal No. 1 of 1990, decided on 1 January, 1990."""

CITED_FIRST = """1999 CLC 1 [Karachi] referred.
Mst. ALI---Petitioner
versus
THE STATE---Respondent
Civil Appeal No. 1 of 2020, decided on 1st January, 2020."""

EXPECTED_CITED_TITLE = "Mst. ALI versus THE STATE"

HEADNOTE_ONLY = """1990 CLC 1 [Peshawar] Before X, J
Bashir Ahmad versus Crown
Civil Revision No.1 of 1990, decided on 1 January, 1990."""


def test_order_sheet_title_exact_from_header_parties():
    assert case_title_from_order_sheet(HEADER) == EXPECTED_HEADER_TITLE
    assert is_valid_order_sheet_case_title(EXPECTED_HEADER_TITLE)


def test_order_sheet_ignores_headnote_versus_line():
    assert case_title_from_order_sheet(HEADNOTE_TRAP) == "Mst. KAUSAR BIBI versus MUHAMMAD MUSHTAQ"
    assert "Bashir Ahmad" not in (case_title_from_order_sheet(HEADNOTE_TRAP) or "")
    assert "Crown" not in (case_title_from_order_sheet(HEADNOTE_TRAP) or "")


def test_order_sheet_title_parties_with_role_suffixes():
    assert case_title_from_order_sheet(CITED_FIRST) == EXPECTED_CITED_TITLE


def test_order_sheet_title_none_without_role_markers():
    assert case_title_from_order_sheet(HEADNOTE_ONLY) is None


APPELLANT_MULTILINE = """2020 CLC 10 [Lahore] Before J, J
MUHAMMAD
ALI KHAN---Appellant
versus
THE STATE---Respondent
Criminal Appeal No. 1 of 2020, decided on 1st January, 2020."""

EXPECTED_APPELLANT = "MUHAMMAD ALI KHAN versus THE STATE"

PLAINTIFF_BLOCK = """2020 YLR 2 Before K, J
Jane Doe---Plaintiff
versus
ACME CORP---Defendant
Suit No. 1 of 2020, decided on 2nd February, 2020."""

EXPECTED_PLAINTIFF = "Jane Doe versus ACME CORP"


def test_order_sheet_title_appellant_multiline_exact():
    assert case_title_from_order_sheet(APPELLANT_MULTILINE) == EXPECTED_APPELLANT


def test_order_sheet_title_plaintiff_defendant_exact():
    assert case_title_from_order_sheet(PLAINTIFF_BLOCK) == EXPECTED_PLAINTIFF


def test_mutant_m11_strips_respondent_dots_suffix():
    text = """Before J, J
Foo Bar---Petitioner
versus
Baz Qux---Respondents
Civil Appeal No. 1 of 2020, decided on 1st January, 2020."""
    assert case_title_from_order_sheet(text) == "Foo Bar versus Baz Qux"
