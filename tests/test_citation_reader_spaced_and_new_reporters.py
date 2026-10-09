"""PakistanLawSite pages print their own citation with spaced reporters ("P L D 2025 Lahore 98", "2025 S C M R 1967",
"2025 P L C 45"), and the reader matched only the run-together forms and had no PLC/CLD/GBLR pattern, so every PLD
and PLC search-harvest row was quarantined "no citation supported by source" (PR #169, 8 October 2026). The forms
the reader already handled must keep their exact canonical strings, since stored judgments are keyed on them."""

from __future__ import annotations

import pytest

from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.parsers.citation_extractor import extract_citations, normalise_citation


def _norms(text):
    return [c["normalized"] for c in extract_citations(text)]


@pytest.mark.parametrize(
    "raw, expected",
    [
        # unchanged: exact strings stored before this change
        ("PLD 2023 SC 123", "PLD 2023 SC 123"),
        ("pld 2023 sc 123", "PLD 2023 SC 123"),
        ("PLD 2020 Lahore 1", "PLD 2020 Lahore 1"),
        ("2023 SCMR 456", "2023 SCMR 456"),
        ("2023 SCMR 456 (SC)", "2023 SCMR 456"),
        ("2023 CLC (Lahore) 789", "2023 CLC (Lahore) 789"),
        ("2023 CLC 789", "2023 CLC 789"),
        ("2022 PCr.LJ 100", "2022 PCrLJ 100"),
        ("2025 P Cr. L J 1756", "2025 PCrLJ 1756"),
        ("2021 MLD 55", "2021 MLD 55"),
        ("2020 YLR 10", "2020 YLR 10"),
        ("2019 YLR (Lahore) 5", "2019 YLR (Lahore) 5"),
        ("2023 PTD 200", "2023 PTD 200"),
        ("2023 PTCL 150", "2023 PTCL 150"),
        ("2023 PCLD 7", "2023 PTCL 7"),
        # spaced reporters, as PakistanLawSite prints them
        ("P L D 2025 Lahore 98", "PLD 2025 Lahore 98"),
        ("P.L.D. 2025 Peshawar 97", "PLD 2025 Peshawar 97"),
        ("2025 S C M R 1967", "2025 SCMR 1967"),
        ("2025 C L C 7", "2025 CLC 7"),
        ("2025 Y L R 300", "2025 YLR 300"),
        ("2025 M L D 12", "2025 MLD 12"),
        ("2025 P T D 9", "2025 PTD 9"),
        ("2025 P.Cr.L.J. 42", "2025 PCrLJ 42"),
        ("2025 P T C L 150", "2025 PTCL 150"),
        ("2025 P.T.C.L. 3", "2025 PTCL 3"),
        ("2023 PTCLR 9", "2023 PTCL 9"),
        # multi-word PLD courts (verbose-mode spaces never matched them)
        ("P L D 2026 Supreme Court 1", "PLD 2026 SC 1"),
        ("PLD 2023 Federal Shariat Court 45", "PLD 2023 FSC 45"),
        ("P L D 2026 Lahore High Court 3", "PLD 2026 Lahore 3"),
        ("P L D 2026 FCC 1", "PLD 2026 FCC 1"),
        ("P L D 2026 Supreme Court (AJ&K) 1", "PLD 2026 SC (AJ&K) 1"),
        # the AJ&K High Court keeps the key bare "AJ&K" always had ("Aj&K"); only its Supreme Court is apart
        ("P L D 2026 High Court (AJ&K) 1", "PLD 2026 Aj&K 1"),
        ("PLD 2023 AJ&K 1", "PLD 2023 Aj&K 1"),
        ("PLD 2023 AJK 1", "PLD 2023 AJ&K 1"),
        ("2023 CLC (AJ&K) 1", "2023 CLC (Aj&K) 1"),
        ("2023 YLR (AJ&K) 5", "2023 YLR (Aj&K) 5"),
        ("2023 CLC (A.J.&K.) 3", "2023 CLC (A.J.&K.) 3"),
        # reporters the reader did not know
        ("2025 PLC 123", "2025 PLC 123"),
        ("2025 P L C 123", "2025 PLC 123"),
        ("2025 P L C (C.S.) 45", "2025 PLC (CS) 45"),
        ("2025 PLC(CS) 45", "2025 PLC (CS) 45"),
        ("2025 CLD 1234", "2025 CLD 1234"),
        ("2025 C L D 12", "2025 CLD 12"),
        ("2025 GBLR 45", "2025 GBLR 45"),
        # note series
        ("2019 YLR Note 120", "2019 YLR Note 120"),
        ("2019 YLR N 120", "2019 YLR Note 120"),
        ("2018 P Cr. L J Note 41", "2018 PCrLJ Note 41"),
        ("2020 P L C Note 5", "2020 PLC Note 5"),
    ],
)
def test_citation_forms(raw, expected):
    assert _norms(raw) == [expected]
    assert normalise_citation(raw) == expected


def test_a_spaced_pld_page_takes_its_own_citation_not_a_cited_case():
    """Staging 6404c66e: body "P L D 2025 Peshawar 97 ..." was promoted as 2006 SCMR 631, a case its headnote cites."""
    body = (
        "P L D 2025 Peshawar 97 Before Ishtiaq Ibrahim, C.J. MUHAMMAD ALI---Petitioner Versus The STATE---Respondent "
        "Criminal Revision No. 12 of 2024, decided on 3rd March, 2025. Bail---Further inquiry. Muhammad Ahmad Ameen "
        "2006 SCMR 631 ref. " + "Text of the judgment. " * 80
    )
    out = extract_judgment_deterministic(html=None, text=body)
    assert out["citations"][0] == "PLD 2025 Peshawar 97"
    assert "2006 SCMR 631" not in out["citations"]
    assert "2006 SCMR 631" in out["citations_cited"]


def test_a_headnote_citation_marked_ref_is_never_the_pages_own():
    body = "Head note: Bail---Further inquiry. Muhammad Ahmad Ameen 2006 SCMR 631 ref. " + "Text. " * 200
    out = extract_judgment_deterministic(html=None, text=body)
    assert "2006 SCMR 631" not in out["citations"]
    assert "2006 SCMR 631" in out["citations_cited"]


@pytest.mark.parametrize(
    "grid, page, expected",
    [
        # the grid prints PLD without its court; PLD restarts page numbers per court, so the page's own
        # "P L D 2026 Lahore 7" is the identity (45 distinct PLD judgments were dropped as duplicates)
        ("2026 PLD 7", ["PLD 2026 Lahore 7"], "PLD 2026 Lahore 7"),
        ("PLD 2026 7", ["PLD 2026 Sindh 7"], "PLD 2026 Sindh 7"),
        # a court-form page citation for a different page is not this judgment's
        ("2026 PLD 7", ["PLD 2026 Lahore 8"], "2026 PLD 7"),
        # other reporters keep the grid row's citation
        ("2025 SCMR 1967", ["2025 SCMR 1967"], "2025 SCMR 1967"),
        ("2025 PLC 45", [], "2025 PLC 45"),
        (None, ["PLD 2025 Lahore 98"], "PLD 2025 Lahore 98"),
    ],
)
def test_pls_own_citation_prefers_the_pages_court_form_for_pld(grid, page, expected):
    from scraper.tasks.promotion import pls_own_citation

    assert pls_own_citation(grid, page) == expected
