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
from pathlib import Path

from scraper.parsers.statute_parser import (
    _is_amendment_statute,
    _ocr_column_cleanup_applies,
    split_into_sections,
)
from scraper.statute_quality.section_reextract import (
    REEXTRACT_MEGA_SECTION_CHARS,
    gate_section_reextract,
)

COTTON = "The Balochistan Cotton Control (Amendment) Act, 2004"
EDU = "The Balochistan Government Educational and Training Institutions (Amendment) Act No. III of 2007"
LAND = "The Balochistan Land Revenue (Amendment) Act No. XXV of 2022"
WAGES = "The Balochistan Payment of Wages Act, 2021"
_FIXTURES = Path(__file__).resolve().parents[0] / "fixtures" / "statute_quality"
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


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


def test_edu_ocr_wrapped_margin_notes_are_not_sections_4_and_8():
    """Instrument name wraps past the next line, and ``1.`` OCR'd as ``t (1)``.

    The keyword splitter used to keep principal sections 4 and 8 and drop the
    short title. The Board list inside the substituted section 4 stays in that
    clause. It is not an amending-act section.
    """
    sections = split_into_sections(_fixture("edu_institutions_amendment_2007_ocr.txt"), EDU)
    numbers = [section["section_number"] for section in sections]
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert numbers == [str(n) for n in range(1, 6)]
    assert "may be called" in bodies["1"]
    assert "come into force" in bodies["1"]
    assert "Ordinsace" in bodies["3"]
    assert "Board shall consist" in bodies["3"]
    assert not any(_MARGIN_BODY.match(section["section_text"]) for section in sections)
    assert "Section 6 shall be omitted" in " ".join(bodies.values())
    assert "Vice-Chairman" in " ".join(bodies.values())
    assert all(len(section["section_text"]) < REEXTRACT_MEGA_SECTION_CHARS for section in sections)
    gate = gate_section_reextract(
        [
            {"section_number": str(n), "section_text": f"section {n} of Ordinance XI of 1960. Margin note."}
            for n in (4, 6, 8)
        ],
        sections,
    )
    assert gate.verdict == "PASS"
    assert gate.may_apply is True


def test_own_line_ocr_clause_numbers_stay_monotonic():
    """CamScanner puts ``l.`` and ``2`` on their own lines, apart from the clause.

    That is the cotton collapse: the margin notes drop, then the Act's own
    numbers never become headings, and the rest of the Act falls into section 2.
    """
    text = """
Short title and
l.
(1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004.
(2) It shall come into force at once.
2
In the said Ordinance, in section 2, for the word cotton the word lint shall be substituted and the Board shall notify the change.
3
In the principal Act, in section 5, the collector shall record the mutation and publish it in the Gazette.
"""
    sections = split_into_sections(text, COTTON)
    numbers = [section["section_number"] for section in sections]
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert numbers == ["1", "2", "3"]
    assert "may be called" in bodies["1"]
    assert "come into force" in bodies["1"]
    assert "word lint shall be substituted" in bodies["2"]
    assert "record the mutation" in bodies["3"]
    assert "word lint" not in bodies["1"]
    assert not any(_MARGIN_BODY.match(section["section_text"]) for section in sections)
    assert all(len(section["section_text"]) < REEXTRACT_MEGA_SECTION_CHARS for section in sections)


def test_inline_l_dot_short_title_is_section_one():
    """``Short title and l. (1)`` is section 1, and the next bare digit is clause 2."""
    text = """
Short title and l. (1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004.
(2) It shall come into force at once.
2
In the said Ordinance, in section 9, for the word cotton the word lint shall be substituted and the Board shall notify the change.
"""
    bodies = _bodies(text)

    assert list(bodies) == ["1", "2"]
    assert "may be called" in bodies["1"]
    assert "come into force" in bodies["1"]
    assert bodies["2"].lstrip().startswith("2")
    assert "word lint shall be substituted" in bodies["2"]


def test_inthe_said_is_its_own_clause():
    """``15. Inthe said`` is the section 17 clause, not the tail of the clause above it."""
    text = """
1. (1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004 and it shall come into force at once.
14. In the said Ordinance, in section 16, no variety except the one allowed shall be grown and the Inspector shall record it.
15. Inthe said Ordinance, in section 17, for the figures 10 percent the figures 8.5 percent shall be substituted and the Board shall notify the change.
16. In the said Ordinance, in section 19, the offences shall be cognizable and the Inspector shall prosecute them.
"""
    bodies = _bodies(text)
    host = next(body for body in bodies.values() if "8.5 percent" in body)
    assert "section 17" in host.lower()
    assert "variety" not in host.lower()
    assert "cognizable" not in host.lower()


def test_page_number_above_a_numbered_in_the_said_line_stays_a_page_number():
    """A scanned page digit above ``(3) In the said`` is not clause 1 of the next page."""
    text = """
1. (1) This Act may be called the Balochistan Land Revenue (Amendment) Act, 2022 and it shall come into force at once.
Amendment of (2) In the Balochistan Land Revenue Act, 1967, in section 4, the collector shall record the mutation and publish it in the Gazette.
1

Insertion of new (3) In the said Act, after section 41, the following new section shall be inserted and the Board shall notify the digitized record.
"""
    bodies = _bodies(text, LAND)
    assert list(bodies) == ["1", "2", "3"]
    assert "record the mutation" in bodies["2"]
    assert "section 41" in bodies["3"]
    assert not bodies["3"].lstrip().startswith("1.")


def test_named_principal_preamble_does_not_take_the_amendment_path():
    """``An Act to further amend`` in a named principal Act is the recital."""
    text = """
An Act to further amend certain laws relating to income tax.
1. Short title. This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Rates. The schedule applies to every person and the tax shall be collected as provided in this Act and notified in the Gazette.
"""
    assert _is_amendment_statute("Finance Act, 1985", text) is False
    assert _is_amendment_statute(
        "The Balochistan Cotton Control Act, 2004",
        "1. (1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004 and it shall come into force at once.",
    ) is True


def test_clean_principal_act_is_byte_identical_when_ocr_cleanup_is_off(monkeypatch):
    """A clean Finance Act must not lose ``sub-`` / ``Sch`` to the column cleanup.

    With the gate off, the public split matches a split that never runs the
    cleanup. Forcing the cleanup on the same text joins those lines.
    """
    import scraper.parsers.statute_parser as parser

    text = """
1. Short title, extent and commencement.— (1) This Act may be called the Finance Act, 1985. (2) It extends to the whole of Pakistan.
2. Definitions.— In this Act, sub-
section means a numbered part of a section and the Board shall notify that meaning in the Gazette.
3. Rates.— The schedule applies to every person and the tax shall be collected as provided in this Act.
Sch
eduled rates are published with this Act and shall remain in force for the tax year.
"""
    name = "Finance Act, 1985"
    assert _ocr_column_cleanup_applies(text, name) is False

    def rows(sections):
        return [(section["section_number"], section["section_text"]) for section in sections]

    published = rows(split_into_sections(text, name))
    monkeypatch.setattr(parser, "_ocr_column_cleanup_applies", lambda *_args, **_kwargs: False)
    baseline = rows(parser.split_into_sections(text, name))
    assert published == baseline
    blob = "\n".join(body for _number, body in published)
    assert "sub-\nsection" in blob
    assert "Scheduled" not in blob

    monkeypatch.setattr(parser, "_ocr_column_cleanup_applies", lambda *_args, **_kwargs: True)
    forced = "\n".join(body for _number, body in rows(parser.split_into_sections(text, name)))
    assert "subsection" in forced
    assert "Scheduled" in forced
    assert forced != blob


def test_page_number_does_not_split_a_clause_that_already_has_its_number():
    """A CamScanner page number sits above the next line of the same clause."""
    text = """
2
Short title and 1. (1) This Act may be called the Balochistan Cotton Control (Amendment) Act, 2004 and it shall come into force at once.
3
"(5) An Inspector appointed under this Ordinance shall record the seizure and report it to the Board within seven days.
4.. In the said Ordinance, in section 7, for the word cotton the word lint shall be substituted and the Board shall notify the change.
"""
    bodies = _bodies(text)

    assert list(bodies) == ["1", "2"]
    assert "may be called" in bodies["1"]
    assert "record the seizure" in bodies["1"]
    assert "word lint shall be substituted" in bodies["2"]


def test_cotton_ocr_bare_and_garbled_clause_numbers_do_not_collapse():
    """Margin notes are ignored. ``3 In the said`` and ``4..`` and ``ve`` still split.

    Stored cotton was 18 margin-note rows. A two-row re-extract is a collapse
    and section 2 holds the rest of the Act.
    """
    text = _fixture("cotton_amendment_2004_ocr.txt")
    sections = split_into_sections(text, COTTON)
    numbers = [section["section_number"] for section in sections]
    joined = "\n".join(section["section_text"] for section in sections)

    assert numbers == [str(n) for n in range(1, 24)]
    assert "may be called" in joined
    assert "section 5" in joined.lower()
    # ``15. Inthe said`` is the clause for principal section 17. It must not
    # stay glued to the previous clause (the variety/seed amendment).
    moisture = next(
        section["section_text"] for section in sections if re.search(r"(?i)section\s+17\b", section["section_text"])
    )
    assert "variety" not in moisture.lower()
    assert not any(_MARGIN_BODY.match(section["section_text"]) for section in sections)
    assert all(len(section["section_text"]) < REEXTRACT_MEGA_SECTION_CHARS for section in sections)
    before = [
        {"section_number": str(n), "section_text": f"section {n} of W.P. Ordinance XX of 1966. Operative words."}
        for n in (2, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 26, 27)
    ]
    gate = gate_section_reextract(before, sections)
    assert gate.verdict == "PASS"
    assert gate.may_apply is True


def test_land_text_layer_parenthetical_clauses_stay_sequential():
    """pdfplumber glues the marginal note to ``(N) In the said Act`` and drops ``N.``.

    Page numbers and the distribution list must not become the section numbers.
    """
    sections = split_into_sections(_fixture("land_revenue_amendment_2022_pdfplumber.txt"), LAND)
    numbers = [section["section_number"] for section in sections]
    bodies = {section["section_number"]: section["section_text"] for section in sections}
    joined = "\n".join(bodies.values())

    assert numbers == [str(n) for n in range(1, 11)]
    assert "may be called" in bodies["1"]
    assert "come into force" in bodies["1"]
    assert "Digitized periodical records" in joined
    assert "41" not in numbers
    assert "Senior Member" not in joined
    assert "Principal Secretary" not in joined
    assert not any(_MARGIN_BODY.match(section["section_text"]) for section in sections)
    assert all(len(section["section_text"]) < REEXTRACT_MEGA_SECTION_CHARS for section in sections)
    gate = gate_section_reextract(
        [
            {"section_number": str(n), "section_text": f"section {n} of the Act No. XVII of 1967. Margin note."}
            for n in (4, 41, 39, 42)
        ],
        sections,
    )
    assert gate.verdict == "PASS"
    assert gate.may_apply is True


def test_land_ocr_layer_finds_the_short_title_not_principal_section_numbers():
    """The same PDF's OCR layer must not store principal sections 4, 41 and 39."""
    sections = split_into_sections(_fixture("land_revenue_amendment_2022_ocr.txt"), LAND)
    numbers = [section["section_number"] for section in sections]
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert numbers == [str(n) for n in range(1, 5)]
    assert "may be called" in bodies["1"]
    assert not any(number in numbers for number in ("39", "41", "42"))
    assert not any(_MARGIN_BODY.match(section["section_text"]) for section in sections)
    gate = gate_section_reextract(
        [
            {"section_number": str(n), "section_text": f"section {n} of the Act No. XVII of 1967. Margin note."}
            for n in (4, 41, 42)
        ],
        sections,
    )
    assert gate.verdict == "PASS"
    assert gate.may_apply is True


def test_wages_principal_act_is_not_split_as_an_amendment():
    """Payment of Wages is the principal Act. Cross-refs and ``|`` are OCR noise.

    A short re-extract stays quarantined as ``ocr_heading_noise`` until the
    sections are a long monotonic run. It is not an amendment-margin split.
    """
    text = _fixture("payment_of_wages_2021_ocr.txt")
    sections = split_into_sections(text, WAGES)
    numbers = [section["section_number"] for section in sections]
    joined = "\n".join(section["section_text"] for section in sections)

    assert "|" not in joined
    assert "Short title" in joined
    assert "may be called" in joined
    assert not any(_MARGIN_BODY.match(section["section_text"]) for section in sections)
    before = [
        {"section_number": str(n), "section_text": f"section {n} of the Factories Act, 1934. Cross reference only."}
        for n in range(1, 6)
    ]
    gate = gate_section_reextract(before, sections)
    assert numbers == ["1", "2", "3", "5", "6", "7"]
    assert gate.verdict == "FAIL"
    assert gate.reason == "ocr_heading_noise"
    assert gate.may_apply is False
