"""Footnote dates DD.MM.YYYY must not become section headings.

Auditor 2026-10-27. law.gok.pk volume notes such as ``11.10.2018. 278 Volume XI``
were split as section 11 and, because that number was seen first, the real
section 11 was dropped. Day and month numbers are small, so the 1800–2099
year-heading rule does not catch them.
"""

import re
from pathlib import Path

from scraper.parsers.statute_parser import section_text_starts_with_amendment_date, split_into_sections

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "statute_quality" / "bank_ajk_amendment_date.txt"
DATE_LINE_RE = re.compile(r"^\s*\d{1,2}\.\d{1,2}\.(?:19|20)\d{2}\b")


def _by_number(text: str, name: str) -> dict:
    sections = split_into_sections(text, name)
    return {section["section_number"]: section["section_text"] for section in sections}


def test_amendment_date_line_is_not_a_heading_and_a_year_line_is_a_different_class():
    assert section_text_starts_with_amendment_date("11.10.2018. 278 Volume XI (2001-2010) proportion to the shares")
    assert section_text_starts_with_amendment_date("17.02.2007. 2 In subsection (1) for the words")
    assert section_text_starts_with_amendment_date("  2.03.2007. note")
    assert section_text_starts_with_amendment_date("23.03.2019. 277 Volume XI (2001-2010) discovered to have not been qualified")
    assert not section_text_starts_with_amendment_date("11. Capital of the Bank.- The capital of the Bank shall be divided.")
    assert not section_text_starts_with_amendment_date("2018. A year heading is the 1800-2099 class, not a footnote date.")


def test_bank_ajk_fixture_keeps_real_sections_and_rejects_date_lines():
    text = FIXTURE.read_text(encoding="utf-8")
    bodies = _by_number(text, "The Bank of Azad Jammu & Kashmir Act, 2005")

    assert "2018" not in bodies
    assert "2007" not in bodies
    assert "2019" not in bodies
    for text_body in bodies.values():
        assert DATE_LINE_RE.match(text_body) is None

    assert "Capital of the Bank" in bodies["11"]
    assert "other subscribers of the Bank" in bodies["11"]
    assert "audited by auditors" in bodies["17"]
    assert "discovered to have not been qualified for the office" in bodies["23"]
    assert "annual report of the Bank" in bodies["18"]
    assert "keep minutes of every meeting" in bodies["20"]
    assert "may make rules" in bodies["25"]
    assert "earlier instrument" in bodies["26"]
    assert "Shares of the Bank" in bodies["10"]
    assert "278 Volume XI" in "\n".join(bodies.values())
    assert "Substituted by the amending note" in "\n".join(bodies.values())


def test_date_line_after_real_heading_stays_inside_that_section():
    text = """
10. Shares of the Bank.- The authorised capital shall be divided among the subscribers in the manner set out in this section of the Act.
11. Capital of the Bank.- The capital of the Bank shall be divided in proportion to the shares held by the Government and the other subscribers of the Bank.
11.10.2018. 278 Volume XI (2001-2010) proportion to the shares of the Bank shall stand revised from that amending note.
12. General reserve.- The Bank shall maintain a general reserve from its annual profits in the manner prescribed by this section.
"""
    bodies = _by_number(text, "The Bank of Azad Jammu & Kashmir Act, 2005")
    assert set(bodies) == {"10", "11", "12"}
    assert "Capital of the Bank" in bodies["11"]
    assert "278 Volume XI" in bodies["11"]
    assert DATE_LINE_RE.match(bodies["11"]) is None


def test_single_digit_day_date_does_not_replace_section_2():
    text = """
1. Short title.- This Act may be called the Bank of Azad Jammu and Kashmir Act, 2005 and it extends to Azad Jammu and Kashmir.
2.03.2007. 2 In subsection (1) for the words the Bank the following words shall be read in this amending note.
2. Definitions.- In this Act, unless there is anything repugnant in the subject or context, Bank means the Bank established under this Act.
3. Commencement.- This Act shall come into force on the day it is published in the official Gazette of the territory.
"""
    bodies = _by_number(text, "The Bank of Azad Jammu & Kashmir Act, 2005")
    assert "Bank means the Bank" in bodies["2"]
    assert DATE_LINE_RE.match(bodies["2"]) is None
    assert all(DATE_LINE_RE.match(body) is None for body in bodies.values())
