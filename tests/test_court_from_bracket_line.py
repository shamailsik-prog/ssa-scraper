"""Year-first reporters (P.Cr.L.J., PLC, YLR, MLD, CLC ...) print the court in brackets on the line after the
citation, e.g. "1983 P Cr. L J 2056 [Karachi]". Their citation carries no court, so before this the court was
read only from a short list of full court names in the text and these pages were held as "court unknown"
(911 P.Cr.L.J. and 31 PLC pages on 10 Oct 2026)."""

import pytest

from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.parsers.citation_extractor import court_from_bracket

KARACHI_PCRLJ = (
    "1983 P Cr. L J 2056   [Karachi]   Before Z. C. Valiani, J   ABDUL RAZZAK-Appellant   Versus   THE STATE-Respondent"
    "   Criminal Appeal No. 23 of 1977, decided on 17th April, 1983,   Penal Code (XLV of 1860)-   --- S. 471"
)
NUMBERED_PCRLJ = (
    "1983 P Cr. LJ 2039 (2)   [Karachi]   Before B. G. N. Kazi and Fakhruddin H. Shaikh, JJ   MUHAMMAD ANWAR---Appellant"
    "   Versus   THE STATE Respondent   Criminal Appeal No. 51, Confirmation Case No. 6 of 1982 (Sukkur)"
)


@pytest.mark.parametrize(
    "text, expected",
    [
        (KARACHI_PCRLJ, "Karachi"),
        (NUMBERED_PCRLJ, "Karachi"),
        ("2024 YLR 812   [Lahore]   Before Ali Baqar Najafi, J   A-Petitioner Versus B-Respondent", "Lahore"),
        ("2021 MLD 77 [Peshawar High Court] Before X, J  A versus B", "Peshawar"),
        ("2019 P Cr. L J 10 [Federal Shariat Court] Before X, J  A versus B", "FSC"),
        ("2018 CLC 5 [Quetta] Before X, J  A versus B", "Balochistan"),
    ],
)
def test_bracketed_court_after_citation_sets_court(text, expected):
    out = extract_judgment_deterministic(html=None, text=text)
    assert out["court"] == expected


def test_tribunal_in_brackets_is_kept_as_written():
    text = "2025 PLC 41 [Punjab Labour Appellate Tribunal] Before X, Chairman  A versus B"
    out = extract_judgment_deterministic(html=None, text=text)
    assert out["court"] == "Punjab Labour Appellate Tribunal"


@pytest.mark.parametrize(
    "text",
    [
        "1983 P Cr. L J 2056 [2] Before X, J  A versus B",  # a footnote marker, not a court
        "1983 P Cr. L J 2056 Before X, J  A versus B [emphasis added]",  # brackets in the body, not a court
    ],
)
def test_non_court_brackets_are_ignored(text):
    assert court_from_bracket(text, 0) is None


def test_bracket_far_from_the_citation_is_ignored():
    text = "1983 P Cr. L J 2056 Before X, J " + "words " * 80 + "[Karachi]"
    assert court_from_bracket(text, 0) is None


def test_row_court_still_wins_over_the_bracket():
    out = extract_judgment_deterministic(html=None, text=KARACHI_PCRLJ, source_meta={"court": "Sindh High Court"})
    assert out["court"] == "Sindh High Court"


def test_pld_court_from_citation_unchanged():
    out = extract_judgment_deterministic(html=None, text="PLD 2025 Lahore 98 [Lahore] Before X, J  A versus B")
    assert out["court"] == "Lahore"
