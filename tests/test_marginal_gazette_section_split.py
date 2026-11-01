"""Balochistan gazette columns must not collapse an act into one section.

pdfplumber reads the marginal note and the section number as one line
(``Short title, 1. (1) This Act…``). A line-start split then keeps only the
heading whose note wrapped onto the previous line. For the Animal Slaughter
Control Act that heading is section 2, and sections 1 and 3–15 stay inside it
(reextract_collapse_count 9→1, one ~12.5k row, no real section 1).

A normal line-start act still loses only its distribution-list footer.
"""

from scraper.parsers.statute_parser import split_into_sections
from scraper.statute_quality.distribution_list import strip_distribution_list_footer
from scraper.statute_quality.section_reextract import gate_section_reextract

ANIMAL_SLAUGHTER_SHAPE = """
TO BE PUBLISHED IN THE NEXT ISSUE OF BALOCHISTAN GAZETTE.
It is hereby enacted as follows: -
Short title, 1. (1) This Act may be called the Example Animal Control Act,
2025.
and extent.
2
(2) It extends to the whole of the Province.
(3) It shall come into force at once.
Definitions.
2. In this Act, unless the context otherwise requires, the following expressions
shall have the meaning assigned to them, that is to say: -
(a) "Board" means the Board established under this Act;
(b) "Government" means the Government of the Province.
Restriction on the 3. No person shall slaughter a useful animal.
slaughter of
animals.
(a) unless such animal has been approved for slaughter.
Power to exempt. 4. The Minister may, under special circumstances, exempt any person from this Act.
Penalty. 5. (1) Whoever contravenes this Act shall be punished with fine.
Cognizance. 6. No court shall take cognizance of an offence under this Act except on a complaint.
Repeal and saving. 7. (1) The earlier Control Act, 1963 is hereby repealed.
(2) Anything done under the repealed Act shall continue in force.
TAHIR SHAH KAKAR
Secretary.
No.PAB/Legis: V (42)/2025/9278. Dated Quetta the, 02nd March, 2026.
A copy is forwarded to the Chief Controller, Government Printing Press, Quetta for favor
of publication in Extra-ordinary issue of the Gazette. Fifty copies of the Act may please be supplied.
Copy for information to: -
1) The Principal Secretary to Governor, Balochistan, Quetta.
2) The Principal Secretary to Chief Minister, Balochistan, Quetta.
3) The Secretary, Government of Balochistan, Law & Parliamentary Affairs Department.
4) The Director General, Public Relations, Balochistan, Quetta for favour of publication.
5) The System Analyst, Balochistan Provincial Assembly.
6) The Secretary, Government of Balochistan, Local Government Department.
"""

LINE_START_WITH_FOOTER = """
1. Short title. This Act may be called the Example Roads Act, 2024 and it extends to the whole Province.
2. Definitions. In this Act, Board means the Board established under this Act.
3. Powers. The Board shall exercise the powers conferred by this Act and shall notify them in the official Gazette.
Copy for information to: -
1. The Principal Secretary to Governor Balochistan, Quetta.
2. The Secretary, Government of Balochistan, Law and Parliamentary Affairs Department, Quetta.
3. The System Analyst, Balochistan Provincial Assembly.
"""


def _by_number(sections):
    return {section["section_number"]: section["section_text"] for section in sections}


def _nine_real_sections():
    return [
        {
            "section_number": str(number),
            "section_text": (
                f"{number}. This provision shall apply and is not a distribution-list designation."
            ),
        }
        for number in range(1, 10)
    ]


def test_marginal_gazette_headings_do_not_collapse_into_one_section():
    """The Animal Slaughter shape: marginal notes share the line with the number.

    Section 2 is the only heading that lands at the start of a line. A line-start
    split stores that one row and drops section 1. The re-extract gate must PASS
    against a 9-section body.
    """
    sections = split_into_sections(
        ANIMAL_SLAUGHTER_SHAPE,
        "THE EXAMPLE ANIMAL CONTROL ACT, 2025",
    )
    numbers = [section["section_number"] for section in sections]
    bodies = _by_number(sections)

    assert "1" in numbers
    assert numbers.count("2") == 1
    assert {"3", "4", "5", "6", "7"} <= set(numbers)
    assert len(sections) >= 6
    assert "may be called" in bodies["1"]
    assert "extends to the whole" in bodies["1"]
    assert "Definitions" not in bodies["1"]
    assert bodies["2"].startswith("Definitions.")
    assert "unless the context otherwise requires" in bodies["2"]
    assert "No person shall slaughter" in bodies["3"]
    assert "shall come into force" not in bodies["2"]
    joined = "\n".join(bodies.values())
    assert "Principal Secretary" not in joined
    assert "System Analyst" not in joined
    assert "Fifty copies" not in joined
    assert max(len(section["section_text"]) for section in sections) < 5000

    gate = gate_section_reextract(_nine_real_sections(), sections)
    assert gate.verdict == "PASS"
    assert gate.reason == ""
    assert gate.may_apply is True


def test_ocr_marginal_heading_keeps_section_1_when_period_or_paren_slips():
    """Scanned gazettes OCR the same column layout with a dropped period or '@)'."""
    text = """
ONS ine 1. @) This Act may be called the Example Institute Act, 2025.
(2) It shall extend to the whole Province.
Definitions. 2 (1) In this Act, unless the context otherwise requires, Board means the Board.
Establishment 3. (1) The Government shall establish an Institute under this Act.
"""
    sections = split_into_sections(text, "The Example Institute Act, 2025")
    bodies = _by_number(sections)
    assert "may be called" in bodies["1"]
    assert "extend to the whole" in bodies["1"]
    assert "unless the context otherwise requires" in bodies["2"]
    assert "may be called" not in bodies["2"]
    assert "shall establish" in bodies["3"]


def test_line_start_act_still_strips_distribution_footer_only():
    """A normal footer strip removes addressee rows and keeps sections 1–3."""
    stripped = strip_distribution_list_footer(LINE_START_WITH_FOOTER)
    assert "may be called" in stripped
    assert "Principal Secretary" not in stripped
    assert "System Analyst" not in stripped

    sections = split_into_sections(
        LINE_START_WITH_FOOTER,
        "The Example Roads Act, 2024",
    )
    numbers = [section["section_number"] for section in sections]
    assert numbers == ["1", "2", "3"]
    joined = "\n".join(section["section_text"] for section in sections)
    assert "may be called" in joined
    assert "powers conferred" in joined
    assert "Principal Secretary" not in joined
    assert "System Analyst" not in joined

    # Dropping the three addressee rows is the allowed footer strip (9 stored, 6 kept).
    before = _nine_real_sections()
    gate = gate_section_reextract(before, before[:6])
    assert gate.verdict == "PASS"
    assert gate.reason == ""
