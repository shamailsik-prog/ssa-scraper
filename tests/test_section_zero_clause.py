"""OCR clause '(o)' must not become statute section 0.

senate.gov.pk/uploads/documents/1528868742_798.pdf prints clause (o) as '0)'.
The line-start splitter stored that as section 0. A lone 0 never starts a
section. '0)' / 'o)' / 'O)' / '(0)' inside a running letter-clause list stays
in the preceding section. '1.', '10.', '20.', a letter suffix, a hyphen
closer and a gazette marginal heading stay headings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scraper.parsers.statute_parser import _LINE_START_SECTION_RE, split_into_sections

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "statute_quality" / "ocr_clause_o_as_section_zero.txt"
ACT = "The Islamabad Healthcare Regulation Act, 2018"
CLAUSE = (
    "provide guidelines to the Registration Board for grant, renewal, "
    "suspension or cancellation of registrations"
)


def _bodies(text: str) -> dict:
    sections = split_into_sections(text, ACT)
    return {section["section_number"]: section["section_text"] for section in sections}


def test_line_start_regex_keeps_numbering_hyphen_suffix_and_rejects_zero():
    text = "\n".join(
        [
            "1. Short title stays a heading in this Act.",
            "10. Registration stays a heading in this Act.",
            "20. Penalties stay a heading in this Act.",
            "10A. Appeal stays a heading in this Act.",
            "6- Inspection stays a hyphen-closer heading in this Act.",
            "08. Weekly rest stays a zero-padded heading in this Act.",
            "03) Cuprammonium hydroxide stays a tariff row marker in this Act.",
            "0) provide guidelines must not be a heading in this Act.",
            "0. Zero period must not be a heading in this Act.",
            "0A. Zero letter must not be a heading in this Act.",
            "o) letter o must not be a heading in this Act.",
            "O) letter O must not be a heading in this Act.",
            "(0) paren zero must not be a heading in this Act.",
            "10-A. Supplemental registration stays the numbered heading.",
        ]
    )
    assert [match.group(1) for match in _LINE_START_SECTION_RE.finditer(text)] == [
        "1",
        "10",
        "20",
        "10A",
        "6",
        "08",
        "03",
        "10",
    ]


@pytest.mark.parametrize("marker", ["0)", "o)", "O)", "(0)"])
def test_ocr_o_clause_in_running_list_merges_into_the_preceding_section(marker: str):
    text = FIXTURE.read_text(encoding="utf-8").replace("0) ", f"{marker} ", 1)
    bodies = _bodies(text)

    assert "0" not in bodies
    assert "0A" not in bodies
    assert CLAUSE in bodies["4"]
    assert "(m) monitor and regulate healthcare services" in bodies["4"]
    assert "(n) grant, renew, suspend or cancel licences" in bodies["4"]
    assert "(p) perform such other functions" in bodies["4"]
    assert "zero-letter line is not a heading" in bodies["4"]
    assert "Meetings of the Board" in bodies["5"]
    assert CLAUSE not in bodies["5"]
    assert "enter a healthcare establishment" in bodies["6"]
    assert "keep a register of healthcare professionals" in bodies["10"]
    assert "appeal within thirty days" in bodies["10A"]
    assert "one million rupees" in bodies["20"]
    assert "complaint filed by the Authority" in bodies["21"]


def test_lone_zero_outside_a_clause_list_does_not_start_a_section():
    text = """
2. Definitions.— In this Act, unless there is anything repugnant in the subject or context, Board means the Registration Board established under this Act.
0) provide guidelines to the Registration Board for grant, renewal, suspension or cancellation of registrations of healthcare professionals.
3. Establishment.— The Government shall establish the Authority within ninety days of the commencement of this Act.
"""
    bodies = _bodies(text)
    assert "0" not in bodies
    assert CLAUSE in bodies["2"]
    assert "establish the Authority" in bodies["3"]
