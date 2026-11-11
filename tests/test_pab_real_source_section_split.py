"""Real text-layer and OCR fixtures for the PAB university section split.

The synthetic patterns in ``test_pab_university_section_split`` do not contain
a marginal note ``First Statutes 49.``. These fixtures are the audit texts.
A trailing First Statutes heading is kept as section ``Schedule``. A marginal
note that carries a section number is not that heading.
"""

from pathlib import Path

from scraper.parsers.statute_parser import (
    _LINE_START_SECTION_RE,
    _heading_continues,
    _heading_parts,
    _line_start_is_inner_subclause,
    split_into_sections,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"


def _bodies(name: str, filename: str) -> dict[str, str]:
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    return {
        section["section_number"]: section["section_text"]
        for section in split_into_sections(text, name)
    }

BOLAN = "The Bolan University of Medical & Health Sciences Act No. VII 2017"
UNIVERSITIES = "The Balochistan Universities Act No. XII of 2022."
GWADAR = "The University of Gwadar Act No. XVI of 2018"
LASBELA = "Lasbela University of Agriculture, Water and Marine Sciences Act.No.IX of 2010"
BUITEMS = (
    "The Balochistan University of Information Technology Engineering & "
    "Management Sciences (Amendment) Act, 2015 Act No XII of 2015"
)
UOB = "The University of Balochistan Act No. III of 1996"


def test_bolan_marginal_first_statutes_49_stays_a_section_and_the_schedule_is_kept():
    bodies = _bodies(BOLAN, "pab_bolan_2017.txt")

    assert list(bodies) == [str(number) for number in range(1, 59)] + ["Schedule"]
    assert bodies["49"].startswith("First Statutes 49.")
    assert "Notwithstanding" in bodies["49"]
    assert "Faculties. 1." not in bodies["49"]
    assert bodies["Schedule"].lstrip().startswith("SCHEDULE")
    assert "FIRST STATUTES" in bodies["Schedule"]
    assert "Faculties. 1." in bodies["Schedule"]
    assert "Faculty of Basic Medical Sciences" in bodies["Schedule"]


def test_universities_2022_mid_act_first_statutes_wrap_does_not_cut_later_sections():
    bodies = _bodies(UNIVERSITIES, "pab_universities_2022.txt")

    assert list(bodies) == (
        [str(number) for number in range(1, 20)]
        + [str(number) for number in range(31, 58)]
        + ["Schedule"]
    )
    assert "First Statutes and" in bodies["52"]
    assert "Notwithstanding" in bodies["52"]
    assert "Faculties 1." not in bodies["52"]
    assert bodies["53"]
    assert "THE FIRST STATUTES" in bodies["Schedule"]
    assert "Faculties 1." in bodies["Schedule"]


def test_gwadar_first_statutes_note_does_not_delete_the_sections_after_it():
    bodies = _bodies(GWADAR, "pab_gwadar_2018.txt")

    assert list(bodies) == (
        [str(number) for number in range(1, 17)]
        + ["26", "36"]
        + [str(number) for number in range(40, 58)]
        + ["Schedule"]
    )
    assert "First Statutes etc. 35." in bodies["26"]
    assert "Notwithstanding" in bodies["26"]
    assert "Faculty of Marine" not in bodies["26"]
    assert bodies["36"]
    assert bodies["57"]
    assert "THE FIRST STATUTE" in bodies["Schedule"]
    assert "Faculty of Marine" in bodies["Schedule"]


def test_lasbela_real_text_keeps_section_39_and_the_paren_section_18():
    bodies = _bodies(LASBELA, "pab_lasbela_2010.txt")

    assert list(bodies) == [str(number) for number in range(1, 47)]
    assert "Schedule" not in bodies
    assert "Powers and (18)" in bodies["18"]
    assert "Senate:- 17." in bodies["17"] or bodies["17"].startswith("Senate")
    assert bodies["39"].startswith("First Statutes and 39.")
    assert "Notwithstanding" in bodies["39"]
    assert bodies["46"].startswith("Validation etc. 46.")


def test_buitems_real_amendment_keeps_clauses_7_through_24_except_the_missing_16():
    bodies = _bodies(BUITEMS, "pab_buitems_2015.txt")

    assert list(bodies) == (
        [str(number) for number in range(1, 16)]
        + [str(number) for number in range(17, 25)]
    )
    assert "16" not in bodies
    assert bodies["7"].startswith("Amendment of 7.")
    assert bodies["17"].startswith("Amendment of 17.")
    assert bodies["22"].startswith("Amendment of 22.")
    assert "Transitory Provisions" in bodies["22"]
    assert "amended by" in bodies["22"].lower()


def test_uob_1996_ocr_cannot_yield_sections_1_through_52():
    """The scan's headings are not a usable 1–52 sequence.

    ``Visitation 1.`` is section 11, ``Officers of the «9.`` is section 9,
    and the schedule is ``SCHEDULE,`` / ``i. Faculties`` rather than
    ``1. Faculties``. Forcing 52 sections would invent numbers the OCR
    does not print. The text of those headings stays in the split.
    """
    source = (FIXTURES / "pab_uob_1996_ocr.txt").read_text(encoding="utf-8")
    sections = split_into_sections(source, UOB)
    numbers = [section["section_number"] for section in sections]
    blob = "\n".join(section["section_text"] for section in sections)

    assert numbers == [
        "1", "2", "3", "4", "5", "6", "7", "30", "23", "8", "03", "34", "9", "10", "37",
    ]
    assert "Visitation 1." in source
    assert "Officers of the «9." in source
    assert "Academic Council. 23." in source
    assert "Vice Chancellor. 12." in source
    assert "SCHEDULE," in source
    assert "i. Faculties" in source
    assert "Academic Council. 23." in blob
    assert "Vice Chancellor. 12." in blob
    assert "i. Faculties" in blob
    assert "11" not in numbers
    assert "52" not in numbers
    # Page banners before section 1 are not stored. The operative OCR is.
    assert len(blob) > len(source) * 0.75


def test_toc_first_statutes_and_first_schedule_do_not_wipe_the_act():
    text = """
CONTENTS
FIRST STATUTES
THE FIRST SCHEDULE
1. Short title. This Act may be called the Example University Act, 2024.
2. Definitions. In this Act, Board means the Board established under this Act.
3. Powers. The Board shall exercise the powers conferred by this Act.
"""
    sections = split_into_sections(text, "The Example University Act, 2024")

    assert [section["section_number"] for section in sections] == ["1", "2", "3"]
    assert "Schedule" not in {section["section_number"] for section in sections}
    assert "conferred by this Act" in sections[2]["section_text"]
    assert "Example University Act" in sections[0]["section_text"]


def test_a_backwards_year_is_not_an_inner_subclause():
    text = (
        "1882. The fund shall be invested in the securities named in this "
        "section for the University."
    )
    match = next(_LINE_START_SECTION_RE.finditer(text))

    assert match.group(1) == "1882"
    assert _line_start_is_inner_subclause(text, match, "1882", "2000") is False


def test_untitled_backwards_section_without_shall_stays_a_section():
    text = """
8. Senate. The Senate is the supreme body of the University and it approves the budget each year.
3. The Registrar keeps the roll of graduates for the University and reports that roll to the Senate at the end of the year.
9. (1) The Syndicate shall be the executive body of the University and shall manage its property.
"""
    numbers = [
        section["section_number"]
        for section in split_into_sections(text, "The Example University Act, 2024")
    ]
    bodies = {
        section["section_number"]: section["section_text"]
        for section in split_into_sections(text, "The Example University Act, 2024")
    }

    assert numbers == ["8", "3", "9"]
    assert "roll of graduates" in bodies["3"]
    assert "roll of graduates" not in bodies["8"]


def test_hyphenated_letter_suffix_is_its_own_section_and_continues():
    assert _heading_parts("18-B") == (18, "B")
    assert _heading_parts("18-AA") == (18, "AA")
    assert _heading_continues("18-AA", "18-B") is True
    assert _heading_continues("18C", "18-B") is True

    text = """
18. Senate. The Senate is constituted under this Act for the University.
10. The quorum for a meeting of the Senate shall be one-half of the total membership.
18-B. Power and functions of the Senate.
(1) The Senate shall be the supreme body of the University.
Further power 18C. (1) The Senate shall also approve the budget of the University each year.
18-AA. Additional power. The Senate shall also make statutes for the residences of the University.
"""
    sections = split_into_sections(text, "The Example University Act, 2024")
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert list(bodies) == ["18", "18-B", "18C", "18-AA"]
    assert "quorum for a meeting of the Senate" in bodies["18"]
    assert "supreme body" in bodies["18-B"]
    assert "approve the budget" in bodies["18C"]
    assert "residences of the University" in bodies["18-AA"]
