"""Amendment-act margin notes are not the amending act's section numbers.

Scanned Balochistan gazettes print ``Amendment of section N of W.P.
Ordinance…`` in the margin. OCR lifts that note onto its own line
(``section N of W.P. Ordinance…``, ``S. N of``, ``Sec. N of``, and a
glued ``16O``). The keyword splitter then stores the principal
Ordinance's number and drops the real short title. The amending act's
own clauses are ``N. In the said Ordinance`` and ``N. In the principal
Act`` (a comma is the OCR of that period).
"""

import re

from scraper.parsers.statute_parser import split_into_sections

COTTON = "The Balochistan Cotton Control (Amendment) Act, 2004"
_MARGIN_BODY = re.compile(r"(?i)^\s*(?:section|sec\.|s\.)\s+\d")


def _numbers(text, name=COTTON):
    return [section["section_number"] for section in split_into_sections(text, name)]


def _bodies(text, name=COTTON):
    return {section["section_number"]: section["section_text"] for section in split_into_sections(text, name)}


def test_line_start_margin_notes_do_not_replace_the_short_title():
    """Auditor shape: bodies open on ``section N of`` and ``16O`` is a number.

    The short title and the operative sentences stay. The principal's
    section numbers are not the amending act's section numbers.
    """
    text = """
It is hereby enacted as follows:-
1. (1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004 and it shall come into force at once.
section 9 of W.P. Ordinance XX of 1966. In the said Ordinance, in section 9, for the word cotton the word lint shall be substituted and the Board shall notify the change.
S. 11 of W. P. Ordinance XX of 1966. In the principal Act, in section 11, the following shall be substituted and the rate shall be published.
Sec. 4 of the Land Revenue Act, 1967. In the principal Act, in section 4, the collector shall record the mutation and publish it.
section 16O ofthe said Act. In the said Act, in section 16, the Inspector shall record the seizure and report it within seven days.
"""
    sections = split_into_sections(text, COTTON)
    numbers = [section["section_number"] for section in sections]
    joined = "\n".join(section["section_text"] for section in sections)

    assert "9" not in numbers
    assert "11" not in numbers
    assert "4" not in numbers
    assert "16O" not in numbers
    assert "16" not in numbers
    assert numbers[0] == "1"
    assert "may be called" in joined
    assert "word lint shall be substituted" in joined
    assert "rate shall be published" in joined
    assert "record the mutation" in joined
    assert "record the seizure" in joined
    assert not any(_MARGIN_BODY.match(section["section_text"]) for section in sections)


def test_wrapped_amendment_of_margin_note_stays_with_the_short_title():
    """``Amendment of`` on one line and ``section N of`` on the next is the same note."""
    text = """
It is hereby enacted as follows:-

1. (1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004 and it shall come into force at once.

Amendment of
section 9 of
W.P. Ordinance XX of 1966.
In the said Ordinance, in section 9, for the word cotton the word lint shall be substituted and the Board shall notify the change.

Amendment of section 11 of W.P. Ordinance XX of 1966. In the principal Act, in section 11, the following shall be substituted and the rate shall be published.
"""
    sections = split_into_sections(text, COTTON)
    numbers = [section["section_number"] for section in sections]
    joined = "\n".join(section["section_text"] for section in sections)

    assert numbers == ["1"]
    assert "may be called" in joined
    assert "word lint shall be substituted" in joined
    assert "rate shall be published" in joined
    assert not any(_MARGIN_BODY.match(section["section_text"]) for section in sections)


def test_comma_ocr_amending_clauses_keep_their_own_numbers():
    """``2, In the said Ordinance`` is clause 2, not the margin note's section 9."""
    text = """
It is hereby enacted as follows:-
1. (1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004 and it shall come into force at once.
section 9 of W.P. Ordinance XX of 1966.
2, In the said Ordinance, in section 9, for the word cotton the word lint shall be substituted and the Board shall notify the change.
section 11 of W.P. Ordinance XX of 1966.
3, In the principal Act, in section 11, the following shall be substituted and the rate shall be published in the Gazette.
section 16O of the said Ordinance.
4, In the said Ordinance, in section 16, the Inspector shall record the seizure and report it to the Board within seven days.
"""
    bodies = _bodies(text)

    assert list(bodies) == ["1", "2", "3", "4"]
    assert "may be called" in bodies["1"]
    assert "word lint shall be substituted" in bodies["2"]
    assert "rate shall be published" in bodies["3"]
    assert "record the seizure" in bodies["4"]
    assert "section 9 of" not in bodies["2"]
    assert not any(_MARGIN_BODY.match(body) for body in bodies.values())


def test_period_amending_clauses_still_number_the_amending_act():
    """A real ``2. In the said Ordinance`` stays clause 2 when the margin note shares the page."""
    text = """
It is hereby enacted as follows:-
1. (1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004 and it shall come into force at once.
Amendment of section 9 of W.P. Ordinance XX of 1966.
2. In the said Ordinance, in section 9, for the word cotton the word lint shall be substituted and the Board shall notify the change.
Amendment of section 11 of the principal Act.
3. In the principal Act, in section 11, the following shall be substituted and the rate shall be published in the Gazette.
"""
    bodies = _bodies(text)

    assert list(bodies) == ["1", "2", "3"]
    assert "may be called" in bodies["1"]
    assert bodies["2"].startswith("2. In the said Ordinance")
    assert "word lint shall be substituted" in bodies["2"]
    assert bodies["3"].startswith("3. In the principal Act")
    assert "9" not in list(bodies)
    assert "11" not in list(bodies)


def test_keyword_section_headings_of_an_ordinary_act_stay():
    """``Section N. Title`` is still a heading. ``Section N of an Act`` is not."""
    text = """
Section 1. Short title, extent and commencement. This Act may be called the Sample Roads Act, 1975 and it extends to the whole Province.
Section 2. Definitions. In this Act, Board means the Board established under this Act for the purposes hereinafter appearing.
Section 3. Powers of the Board. The Board shall exercise the powers conferred by this Act and shall notify them in the official Gazette.
"""
    assert _numbers(text, "Sample Roads Act, 1975") == ["1", "2", "3"]


def test_numbered_short_title_act_is_unchanged():
    text = """
1. Short title. This Act may be called the Example Roads Act, 2024 and it extends to the whole Province.
2. Definitions. In this Act, Board means the Board established under this Act.
3. Powers. The Board shall exercise the powers conferred by this Act and shall notify them in the official Gazette.
"""
    bodies = _bodies(text, "The Example Roads Act, 2024")
    assert list(bodies) == ["1", "2", "3"]
    assert "may be called" in bodies["1"]
    assert "Board means the Board" in bodies["2"]


def test_amendment_heading_keeps_the_amending_acts_number():
    """``2. Amendment of section 4.—`` is section 2 of the amending act."""
    text = """
1. (1) This Act may be called the Sample Amendment Act, 1975 and it shall come into force at once.
2. Amendment of section 4.—In the said Act, in section 4, for the word thirty the word sixty shall be substituted and the tribunal shall record that substitution.
3. Repeal.—The Ordinance is hereby repealed and anything done under it shall continue in force.
"""
    bodies = _bodies(text, "The Sample Amendment Act, 1975")
    assert list(bodies) == ["1", "2", "3"]
    assert "for the word thirty the word sixty" in bodies["2"]
    assert "hereby repealed" in bodies["3"]
