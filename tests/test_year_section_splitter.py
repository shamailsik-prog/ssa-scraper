"""Year tokens inside citations must not become section headings."""

from pathlib import Path

from scraper.parsers.statute_parser import split_into_sections

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "statute_quality" / "year_citation_of_1860_and_dt.txt"


def _by_number(text: str, name: str = "Example Act") -> dict:
    sections = split_into_sections(text, name)
    return {section["section_number"]: section["section_text"] for section in sections}


def test_fixture_of_1860_and_dt_stay_inside_real_sections():
    text = FIXTURE.read_text(encoding="utf-8")
    bodies = _by_number(text, "Gwadar Industrial Estates Development Authority Act")

    assert "1860" not in bodies
    assert "2001" not in bodies
    assert "1930" not in bodies
    assert "shall be dealt with in the manner provided" in bodies["25"]
    assert "Act XLV of" in bodies["25"]
    assert "1860)" in bodies["25"]
    assert "dt. 16.12.2001" in bodies["4"]
    assert "hereinafter referred to as the said Act" in bodies["3"]
    assert "26" in bodies
    assert "5" in bodies


def test_inline_of_1860_does_not_split_section_25():
    text = """
1. Short title.- This Act may be called the Penal Code (Amendment) Act, 1993 and it extends to the whole of Pakistan.
2. Amendment.- In the Pakistan Penal Code (Act XLV of 1860), in section 302, the following shall be substituted and shall have effect immediately.
3. Repeal.- This amending provision repeals the inconsistent notification from the appointed day.
"""
    bodies = _by_number(text, "Penal Code (Amendment) Act, 1993")
    assert "1860" not in bodies
    assert "Act XLV of 1860" in bodies["2"]
    assert "section 302" in bodies["2"]


def test_year_heading_outside_section_sequence_merges_back():
    text = """
1. Short title.- This Act may be called the AJK Shariat Court Act, 1993 and it extends to the territory.
2. Definitions.- In this Act, unless there is anything repugnant in the subject or context, Court means the Court established under this Act.
2000. The words inserted by the amending instrument of the year two thousand shall be read with the principal section and shall have effect.
3. Constitution of the Court.- There shall be a Court to be called the Shariat Court of Azad Jammu and Kashmir.
"""
    bodies = _by_number(text, "AJK Shariat Court Act, 1993")
    assert "2000" not in bodies
    assert "year two thousand" in bodies["2"]
    assert "Constitution of the Court" in bodies["3"]


def test_year_heading_that_continues_section_sequence_is_kept():
    text = """
1798. First rule.- This provision establishes the first operative rule of this long code and shall apply to every proceeding.
1799. Second rule.- This provision establishes the second operative rule of this long code and shall apply to every proceeding.
1800. Third rule.- This provision establishes the third operative rule of this long code and shall apply to every proceeding.
1801. Fourth rule.- This provision establishes the fourth operative rule of this long code and shall apply to every proceeding.
"""
    bodies = _by_number(text, "Long Sequential Code")
    assert set(bodies) >= {"1798", "1799", "1800", "1801"}
    assert "third operative rule" in bodies["1800"].lower()


def test_year_citation_rejected_even_when_it_sits_near_the_section_sequence():
    """Section 1799 makes 1860 look in-sequence (gap 61). The 'of 1860)' citation still is not a heading."""
    text = """
1799. Prior rule.- This provision establishes the prior operative rule and shall apply to every proceeding under this Act.
25. Charge.- Tax is charged under the earlier Code (Act V of
1860) and shall be assessed in the manner set out in the following clauses of this section.
1804. Later rule.- This provision establishes the later operative rule and shall apply to every proceeding under this Act.
"""
    bodies = _by_number(text, "Example Finance Act")
    assert "1860" not in bodies
    assert "shall be assessed in the manner" in bodies["25"]
    assert "prior operative rule" in bodies["1799"].lower()
    assert "later operative rule" in bodies["1804"].lower()
