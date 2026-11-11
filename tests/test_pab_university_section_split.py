"""Balochistan university acts must not promote sub-clauses, years, or statutes.

The PAB OCR path is ``split_into_sections``. A line that is only an inner
numbered clause (``3. The quorum…``) stays inside the section that contains
it. The next marginal heading that continues the sequence (``Power and duties
22.``) is the next section. A citation year is not a section number. The First
Schedule and the First Statutes, including ``1. Faculties``, are not main-Act
sections. A plain numbered act still splits on the same headings as before.
"""

import subprocess
import sys
from pathlib import Path

from scraper.parsers.statute_parser import split_into_sections

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"
UOB = "The University of Balochistan Act No. III of 1996"
LASBELA = "Lasbela University of Agriculture, Water and Marine Sciences Act No. IX of 2010"
TURBAT = "University of Turbat Act No. V of 2012"
BUITEMS = "BUITEMS (Amendment) Act No. XII of 2015"


def _sections(name: str, filename: str):
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    return split_into_sections(text, name)


def _numbers(name: str, filename: str) -> list[str]:
    return [section["section_number"] for section in _sections(name, filename)]


def _bodies(name: str, filename: str) -> dict[str, str]:
    return {section["section_number"]: section["section_text"] for section in _sections(name, filename)}


def test_uob_quorum_subclause_does_not_swallow_the_next_marginal_heading():
    bodies = _bodies(UOB, "pab_uob_act_1996_pattern.txt")
    numbers = list(bodies)

    assert numbers == [str(number) for number in range(1, 24)] + ["Schedule"]
    assert "quorum for a meeting of the Syndicate" in bodies["21"]
    assert "executive body" not in bodies["21"]
    assert "reconstituted" in bodies["3"]
    assert "quorum" not in bodies["3"]
    assert bodies["22"].startswith("Power and duties")
    assert "The Syndicate shall be the executive body" in bodies["22"]
    assert "purchase of immovable property" in bodies["22"]


def test_uob_citation_years_stay_inside_the_section_that_quotes_them():
    bodies = _bodies(UOB, "pab_uob_act_1996_pattern.txt")

    assert "1882" not in bodies
    assert "1925" not in bodies
    assert "1970" not in bodies
    assert "1882" in bodies["22"]
    assert "purchase of immovable property" in bodies["22"]
    assert "1925" in bodies["23"]
    assert "1970" in bodies["23"]
    assert "hereby repealed" in bodies["23"]


def test_uob_first_schedule_faculties_are_not_main_act_sections():
    sections = _sections(UOB, "pab_uob_act_1996_pattern.txt")
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert bodies["4"].startswith("Powers of the University")
    assert "Boards of Studies" not in bodies["4"]
    operative = "\n".join(section["section_text"] for section in sections if section["section_number"] != "Schedule")
    assert "Faculties" not in operative
    assert "Board of each Faculty" not in operative
    assert "Boards of Studies" not in operative
    assert "THE FIRST SCHEDULE" not in operative
    assert "See section" not in operative
    assert "Dean of each Faculty" not in operative
    assert bodies["Schedule"].lstrip().startswith("THE FIRST SCHEDULE")
    assert "1. Faculties" in bodies["Schedule"]
    assert "Dean of each Faculty" in bodies["Schedule"]


def test_lasbela_quorum_subclause_and_citation_years_are_not_sections():
    bodies = _bodies(LASBELA, "pab_lasbela_act_2010_pattern.txt")

    assert list(bodies) == [str(number) for number in range(1, 21)]
    assert "2002" not in bodies
    assert "1925" not in bodies
    assert "2002" in bodies["2"]
    assert "quorum for a meeting of the Syndicate" in bodies["19"]
    assert "executive body" not in bodies["19"]
    assert "consist of the Chancellor" in bodies["3"]
    assert bodies["20"].startswith("Power and duties")
    assert "executive body of the University" in bodies["20"]
    assert "1925" in bodies["20"]
    assert "Commencement of term" in bodies["20"]


def test_turbat_first_statute_tail_is_not_stored_on_section_58():
    bodies = _bodies(TURBAT, "pab_turbat_act_2012_pattern.txt")

    assert list(bodies) == ["57", "58", "Schedule"]
    assert "difficulty arises" in bodies["58"]
    assert "Faculties" not in bodies["58"]
    assert "FIRST STATUE" not in bodies["58"]
    assert "Board of each Faculty" not in bodies["58"]
    assert "1" not in {key for key in bodies if key != "Schedule"}
    assert "FIRST STATUE" in bodies["Schedule"]
    assert "Faculties" in bodies["Schedule"]


def test_buitems_senate_quorum_does_not_swallow_section_18b():
    bodies = _bodies(BUITEMS, "pab_buitems_amendment_2015_pattern.txt")

    assert "10" not in bodies
    assert "2002" not in bodies
    assert "18-B" in bodies
    assert "quorum for a meeting of the Senate" in bodies["18"]
    assert "Power and functions" not in bodies["18"]
    assert "2002" in bodies["12"]
    assert bodies["18-B"].startswith("18-B.")
    assert "Power and functions of the Senate" in bodies["18-B"]
    assert "quorum" not in bodies["18-B"]
    assert "supreme body" in bodies["18-B"]


def test_plain_line_start_act_and_parenthetical_subclause_split_as_before():
    """A normal act keeps forward gaps. ``(3)`` is not a new section."""
    text = """
1. Short title. This Act may be called the Example Roads Act, 2024 and it extends to the whole Province.
2. Definitions. In this Act, Board means the Board established under the Act of 1860 and the rules made thereunder.
3. Powers. The Board shall exercise the powers conferred by this Act and shall notify them in the official Gazette.
(3) The quorum for a meeting of the Board shall be one-half of the members present and voting.
5. Appeal. An appeal shall lie to the Government within thirty days of the order complained of.
"""
    sections = split_into_sections(text, "The Example Roads Act, 2024")
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert list(bodies) == ["1", "2", "3", "5"]
    assert "1860" not in bodies
    assert "Act of 1860" in bodies["2"]
    assert "quorum for a meeting of the Board" in bodies["3"]
    assert "within thirty days" in bodies["5"]


def test_marginal_gazette_run_still_splits_on_each_continuing_heading():
    text = """
Short title 1. (1) This Act may be called the Example Animal Control Act, 2025 and it extends to the Province.
Definitions 2. In this Act, Board means the Board established under this Act for the control of animals.
Penalty 3. Whoever contravenes this Act shall be punished with fine which may extend to fifty thousand rupees.
"""
    sections = split_into_sections(text, "The Example Animal Control Act, 2025")
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert list(bodies) == ["1", "2", "3"]
    assert "may be called" in bodies["1"]
    assert bodies["2"].startswith("Definitions")
    assert "punished with fine" in bodies["3"]


def test_offline_dry_run_prints_section_counts_without_a_database():
    """The report reads fixtures only. The pinned UoB PDF is not in the repo."""
    repo = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(repo / "scripts" / "report_pab_university_sections.py")],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert completed.returncode == 0, completed.stderr
    text = completed.stdout
    uob, rest = text.split("Lasbela University Act IX/2010", 1)
    lasbela, rest = rest.split("University of Turbat Act V/2012", 1)
    turbat, buitems = rest.split("BUITEMS Amendment Act XII/2015", 1)

    assert "no database writes" in text
    assert "section_count: 24" in uob
    assert "Schedule" in uob
    assert "1882" not in uob
    assert "1925" not in uob
    assert "1970" not in uob
    assert "not in repo fixtures" in uob
    assert "398d88c1a9a251e7d08a50dbf3bcd46088afa95ff7a04afef24c3d5dba58e67f" in uob
    assert "section_count: 20" in lasbela
    assert "section_count: 3\n" in turbat
    assert "section_numbers: 57, 58, Schedule" in turbat
    assert "18-B" in buitems
    assert "10" not in buitems.split("section_numbers: ", 1)[1]


def test_a_sentence_about_the_first_schedule_does_not_drop_later_sections():
    text = """
1. Short title. This Act may be called the Example Rates Act, 2024.
2. Rates. The University may set rates. The First Schedule specifies the faculties.
3. Appeal. An appeal shall lie to the Board within thirty days of the order.
"""
    sections = split_into_sections(text, "The Example Rates Act, 2024")
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert list(bodies) == ["1", "2", "3"]
    assert "specifies the faculties" in bodies["2"]
    assert "within thirty days" in bodies["3"]


def test_a_customs_first_schedule_heading_stays_with_the_act():
    text = """
1. Short title. This Act may be called the Example Finance Act, 2024.
2. Customs. In the Customs Act, 1969, the following amendments shall be made.
THE FIRST SCHEDULE
Heading 0101. Live horses, asses and mules, at the rate of twenty percent.
3. Income tax. The rate of tax shall be the rate set out in this section.
"""
    sections = split_into_sections(text, "The Example Finance Act, 2024")
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert list(bodies) == ["1", "2", "3"]
    assert "THE FIRST SCHEDULE" in bodies["2"]
    assert "Live horses" in bodies["2"]
    assert "rate of tax" in bodies["3"]
