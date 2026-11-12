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
    _schedule_cut_follows_last_section,
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

    assert list(bodies) == [str(number) for number in range(1, 61)] + ["Schedule"]
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
        + [str(number) for number in range(21, 58)]
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
        + [str(number) for number in range(18, 31)]
        + [str(number) for number in range(32, 58)]
        + ["Schedule"]
    )
    assert "First Statutes etc. 35." in bodies["35"]
    assert "Notwithstanding" in bodies["35"]
    assert "Faculty of Marine" not in bodies["26"]
    assert "Faculty of Marine" not in bodies["35"]
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


# Printed on the scan as marginal headings. Unprinted numbers (10, 11, 13, 15,
# 28, 30, 33, 34, 37, 38, 42, 51) stay inside the neighbouring section.
_UOB_PRINTED = [
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "12", "14", "16", "17", "18",
    "19", "20", "21", "22", "23", "24", "25", "26", "27", "29", "31", "32",
    "35", "36", "39", "40", "41", "43", "44", "45", "46", "48", "49", "50", "52",
    "Schedule",
]
_UOB_RISING = {
    1, 2, 3, 4, 5, 6, 8, 14, 16, 17, 18, 20, 21, 23, 24, 25, 26, 27, 32, 35,
    36, 40, 41, 44, 45, 46, 48, 50, 52,
}


def test_uob_1996_ocr_keeps_the_printed_rising_headings():
    """The scan prints a rising run of marginal headings. Junk line-starts do not reset it.

    ``Visitation 1.`` is not section 11 and ``30. 0))`` is not section 30.
    Sections the scan does not print are not invented. The statutes tail
    ``SCHEDULE,`` / ``i. Faculties`` / ``2. Denn,`` is section Schedule.
    """
    source = (FIXTURES / "pab_uob_1996_ocr.txt").read_text(encoding="utf-8")
    sections = split_into_sections(source, UOB)
    numbers = [section["section_number"] for section in sections]
    bodies = {section["section_number"]: section["section_text"] for section in sections}
    numeric = [int(number) for number in numbers if number != "Schedule"]

    assert numbers == _UOB_PRINTED
    assert _UOB_RISING <= set(numeric)
    assert numeric == sorted(numeric)
    assert "03" not in numbers
    assert "30" not in numbers
    assert "34" not in numbers
    assert "37" not in numbers
    for absent in (10, 11, 13, 15, 28, 33, 38, 42, 51):
        assert str(absent) not in numbers
    assert bodies["7"].startswith("Teaching in the 7,")
    assert bodies["9"].startswith("Officers of the")
    assert "Registrar" in bodies["14"]
    assert "Vice Chancellor" in bodies["12"]
    assert "Academic Council" in bodies["23"]
    assert bodies["52"].startswith("Removal of 52.")
    assert bodies["Schedule"].lstrip().startswith("SCHEDULE,")
    assert "i. Faculties" in bodies["Schedule"]
    assert "Denn" in bodies["Schedule"]
    assert "2. Denn" not in bodies["52"]


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


IST = "The Institute of Science and Technology Bahawalpur Act, 2018"
AGRI = "The Balochistan University of Agriculture, Quetta Act No VIII of 2021."
SOCIETIES = "Societies Registration Act, 1860"
FINANCE_2020 = "The Finance Act, 2020"
# The Finance Act, 2020 number list on main. Dropping the faculty window
# on ``THE FIRST SCHEDULE`` collapses this to 70.
_FINANCE_2020_NUMBERS = [
    "2", "3", "292", "20", "5", "850", "69", "01", "6", "4", "169", "36I", "1",
    "380", "9", "3102", "71", "84", "04", "0443", "85", "05", "0702", "07",
    "0707", "1006", "1518", "2502", "25", "2507", "2512", "2514", "2521",
    "2602", "2604", "2605", "2614", "2619", "27", "2703", "2705", "28", "2817",
    "2822", "29", "2913", "2942", "31", "3504", "38", "3816", "3817", "3910",
    "4003", "4112", "42", "44", "47", "54", "55", "74", "76", "79", "7", "410",
    "8", "40A", "408", "10", "2710", "2612", "2812", "2813", "232", "2336",
    "2915", "2935", "32U", "3204", "3207", "3806", "3810", "382", "3824",
    "3903", "11", "12", "13", "14", "16", "17", "18", "26", "32",
]
_SOCIETIES_NUMBERS = [
    "1", "2", "3", "4", "5", "12", "6", "7", "8", "9", "10", "13", "74", "15",
    "18", "19", "14", "20", "27", "22", "23", "24", "25", "26", "28", "29",
    "30", "31", "32", "33", "35", "36", "37", "38", "39", "40", "47", "42",
    "43", "45", "48", "49", "50", "51", "53", "54", "55", "56", "57", "58",
    "59", "60",
]


def test_ist_bahawalpur_contents_schedule_does_not_swallow_the_act():
    bodies = _bodies(IST, "pab_ist_bahawalpur_2018.txt")

    assert len(bodies["3"]) > 1000
    assert "Incorporation" in bodies["3"]
    assert "hereby" in bodies["3"]
    assert len(bodies["10"]) > 200
    assert "Visitation" in bodies["10"]
    assert len(bodies["12"]) > 2000
    assert "The Rector" in bodies["12"]
    assert len(bodies["18"]) > 500
    assert "Authorities" in bodies["18"]
    assert len(bodies["24"]) > 1000
    assert bodies["24"].lstrip().startswith("24. Statutes")
    assert len(bodies["25"]) > 1000
    assert "Regulations" in bodies["25"]
    assert "Indemnity" in bodies["40"]
    assert "No suit" in bodies["40"]
    assert len(bodies.get("Schedule", "")) < 20000
    assert "Incorporation" not in bodies.get("Schedule", "")
    assert "Faculties" in bodies["Schedule"]
    assert "Finance and Planning" in bodies["Schedule"]
    assert "may be called the Institute" not in bodies["Schedule"]


def test_toc_that_lists_every_section_does_not_cut_at_schedule():
    bodies = _bodies("The Example Institute Act, 2018", "pab_toc_schedule_synthetic_p.txt")

    assert "body corporate" in bodies["3"]
    assert "award degrees" in bodies["4"]
    assert "may be called the Example Institute Act" in "\n".join(bodies.values())
    assert "body corporate" not in bodies.get("Schedule", "")
    assert "award degrees" not in bodies.get("Schedule", "")


def test_toc_that_lists_every_section_does_not_cut_at_the_first_statutes():
    bodies = _bodies("The Example College Act, 2018", "pab_toc_statutes_synthetic_q.txt")

    assert "Board of the College" in bodies["2"]
    assert "Board of the College" not in bodies.get("Schedule", "")
    assert "Schedule" not in bodies


def test_agriculture_quetta_keeps_sections_1_through_58_and_the_schedule():
    bodies = _bodies(AGRI, "pab_agriculture_quetta_2021.txt")

    assert list(bodies) == [str(number) for number in range(1, 59)] + ["Schedule"]
    assert len(bodies["Schedule"]) > 10000
    assert "FIRST STATUTES" in bodies["Schedule"] or "First Statutes" in bodies["Schedule"]


def test_na_societies_text_stays_at_52_without_the_section_mark_guard():
    """An early OCR ``74`` must not make the s47 margin note look like the tail.

    The longest rising run, not ``max()``, is what keeps sections 48 onward
    when the same-line ``.-`` guard is the only other thing in the way.
    """
    text = (FIXTURES / "pab_na_societies_university.txt").read_text(encoding="utf-8")
    numbers = [section["section_number"] for section in split_into_sections(text, SOCIETIES)]
    note = text.index("First Statutes and Regulations")

    assert numbers == _SOCIETIES_NUMBERS
    assert len(numbers) == 52
    assert _schedule_cut_follows_last_section(text, note) is False


def test_inflated_ocr_number_does_not_approve_a_mid_act_statutes_cut():
    """Synthetic R: ``74`` is not the last section when 1, 2, 5 is the rising run."""
    text = """
1. Short title. This Act may be called the Example University Act, 2018.
2. Definitions. In this Act, Board means the Board of the University.
74. Appointment. The Chancellor shall appoint the Vice Chancellor.
5. Powers. The Board shall exercise the powers conferred by this Act.
First Statutes
and Regulations
7. Savings. Anything done under the repealed Ordinance shall be deemed valid.
8. Repeal. The Ordinance of 2010 is hereby repealed.
"""
    bodies = {
        section["section_number"]: section["section_text"]
        for section in split_into_sections(text, "The Example University Act, 2018")
    }

    assert "deemed valid" in bodies["7"]
    assert "hereby repealed" in bodies["8"]
    assert "deemed valid" not in bodies.get("Schedule", "")
    assert "hereby repealed" not in bodies.get("Schedule", "")


def test_finance_act_2020_keeps_94_sections_past_the_first_schedule():
    text = (FIXTURES / "pab_finance_act_2020.txt").read_text(encoding="utf-8")
    numbers = [section["section_number"] for section in split_into_sections(text, FINANCE_2020)]

    assert numbers == _FINANCE_2020_NUMBERS
    assert len(numbers) == 94
    assert "Schedule" not in numbers
