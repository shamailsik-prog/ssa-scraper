"""Distribution-list footers must not be stored as statute sections.

Balochistan Assembly PDFs end with a "copy forwarded" / "No.PAB/..." addressee
block. Those numbered lines were parsed as sections 1–6. Defect class:
distribution_list_footer_as_sections.
"""

from scraper.document_validation import validate_statute_reconciled_json
from scraper.parsers.statute_parser import split_into_sections
from scraper.statute_quality.distribution_list import strip_distribution_list_footer

TRAFFIC_SHAPE = """
TO BE PUBLISHED IN THE NEXT ISSUE OF BALOCHISTAN GAZETTE.
It is hereby enacted as follows:-
CHAPTER -I
PRELIMINARY
Short title and 1. (1) This Act may be called the Balochistan (Traffic Engineering) Act, 2022.
(2) It extends to the whole of the Balochistan province.
(3) It shall come into force at once.
Overriding effect. 2. This Act shall have overriding effect notwithstanding anything to the contrary contained in any other law for the time being in force.
Definition. 3. (1) In this Act, unless there is anything repugnant in the subject or context, Board means the Board established under this Act.
4. (1) The Government shall, by notification in the Official Gazette, establish a Board to be known as the Balochistan Transport Board.
8. (1) Subject to the other provisions of this Act, the Board shall have the powers to manage and control the affairs of the Bureau.
34. The Bureau may make regulations not inconsistent with the Act or rules made thereunder for carrying out the purposes of this Act.
(TAHIR SHAH KAKAR)
Secretary.
No.PAB/Legis:V(05)/2022/7727. Dated Quetta, the 08th March, 2022.
A copy is forwarded to the Chief Controller, Government Printing Press, Balochistan, Quetta for favour of publication in the next issue of Balochistan Gazette. Before final printing, a copy thereof be sent to this Secretariat for proof reading. Fifty copies of the Act may please be supplied to this Secretariat for record.
(ABDUL REHMAN)
Special Secretary (Legis:)
No.PAB/Legis:V(05)/2022/7727. Dated Quetta, the 08th March, 2022.
A Copy is forwarded for information and necessary action to:-
1. The Principal Secretary to Governor Balochistan, Quetta.
2. The Principal Secretary to Chief Minister Balochistan, Quetta.
3. The Secretary Government of Balochistan, Transport Department, Quetta.
4. The Secretary, Government of Balochistan, Law and Parliamentary Affairs
Department, Quetta.
5. The Director General, Public Relations, Balochistan, Quetta.
6. The System Analyst, Balochistan Provincial Assembly.
7. P.S. to Secretary, Balochistan Provincial Assembly.
Special Secretary (Legis:)
"""

PAREN_LIST = """
1. Short title. This Act may be called the Anti-Terrorism (Balochistan Amendment) Act, 2025 and it shall come into force at once.
2. Amendment of section 2. In the said Act, in section 2, a new clause shall be inserted.
(TAHIR SHAH KAKAR)
Secretary.
No.PAB/Legis: V (18)/2025/ Dated: Quetta, the 05th June, 2025.
Copy for information to: -
1) The Principal Secretary to Governor, Balochistan, Quetta.
2) The Principal Secretary to Chief Minister, Balochistan, Quetta.
3) The Secretary, Government of Balochistan, Health Department, Quetta.
4) The Secretary, Government of Balochistan, Law & Parliamentary Affairs Department,
5) The Director General, Public Relations, Balochistan, Quetta for favour of publication.
6) The System Analyst, Balochistan Provincial Assembly.
SPECIAL SECRETARY (LEGIS:)
"""

SECRETARY_IN_BODY = """
1. Short title, extent and commencement. This Act may be called the Balochistan Example Administration Act, 2024 and it shall extend to the whole of Balochistan.
4. (1) The Board shall comprise of the Chairman and Members.
(a) Secretary Colleges, Higher and Technical Education Department;
(b) Secretary Finance;
(c) Secretary Law;
(d) Additional Secretary who shall be the member/secretary of the Board.
8. Rules. The Secretary, Government of Balochistan, shall notify the rules in the official Gazette within ninety days and lay them before the Provincial Assembly.
9. Chairperson. The Secretary, Government of Balochistan, is the chairperson of the Board for the purposes of this Act.
"""


def _joined(sections) -> str:
    return "\n".join((s.get("section_text") or "") for s in sections)


def test_strip_leaves_real_act_body_and_drops_trailing_addressees():
    stripped = strip_distribution_list_footer(TRAFFIC_SHAPE)
    assert "overriding effect" in stripped
    assert "Balochistan (Traffic Engineering) Act, 2022" in stripped
    assert "establish a Board" in stripped
    assert "make regulations" in stripped
    assert "Principal Secretary" not in stripped
    assert "System Analyst" not in stripped
    assert "Director General, Public Relations" not in stripped
    assert "P.S. to Secretary" not in stripped

    sections = split_into_sections(TRAFFIC_SHAPE, "The Balochistan (Traffic Engineering) Act, 2022")
    blob = _joined(sections)
    assert "establish a Board" in blob
    assert "manage and control the affairs" in blob
    assert "make regulations" in blob
    assert "Principal Secretary" not in blob
    assert "System Analyst" not in blob
    numbers = [s["section_number"] for s in sections]
    assert "4" in numbers
    assert "8" in numbers
    assert "34" in numbers


AT_INLINE_PROVISIONS = """
It is hereby enacted as follows:-
Short title and 1. (1) This Act may be called as the Anti-Terrorism (Balochistan Amendment) Act, 2025.
(2) It shall come into force at once.
(3) It shall have effect to the extent of Balochistan Province.
(4) In the said Act, after clause (m), the following new clause shall be inserted.
1997.
Copy for information to: -
1) The Principal Secretary to Governor, Balochistan, Quetta.
2) The Principal Secretary to Chief Minister, Balochistan, Quetta.
3) The Secretary, Government of Balochistan, Health Department, Quetta.
6) The System Analyst, Balochistan Provincial Assembly.
"""


def test_footer_numbers_do_not_hide_inline_act_provisions():
    """Line-start footer numbers used to win strategy 4 and hide '(1) This Act...'."""
    sections = split_into_sections(AT_INLINE_PROVISIONS, "The Anti-Terrorism (Balochistan Amendment) Act, 2025")
    blob = _joined(sections)
    assert "may be called as the Anti-Terrorism" in blob
    assert "shall come into force at once" in blob
    assert "new clause shall be inserted" in blob
    assert "Principal Secretary" not in blob
    assert "System Analyst" not in blob


def test_paren_distribution_list_is_not_stored_as_sections():
    sections = split_into_sections(PAREN_LIST, "The Anti-Terrorism (Balochistan Amendment) Act, 2025")
    blob = _joined(sections)
    assert "shall come into force at once" in blob
    assert "new clause shall be inserted" in blob
    assert "Principal Secretary" not in blob
    assert "System Analyst" not in blob
    numbers = [s["section_number"] for s in sections]
    assert numbers.count("1") == 1
    assert "6" not in numbers


def test_real_act_that_mentions_a_secretary_is_not_gutted():
    sections = split_into_sections(SECRETARY_IN_BODY, "The Balochistan Example Administration Act, 2024")
    blob = _joined(sections)
    assert "Secretary Finance" in blob
    assert "Secretary Law" in blob
    assert "member/secretary of the Board" in blob
    by_num = {s["section_number"]: s["section_text"] for s in sections}
    assert "8" in by_num
    assert "shall notify the rules" in by_num["8"]
    assert "Secretary, Government of Balochistan" in by_num["8"]
    assert "9" in by_num
    assert "is the chairperson of the Board" in by_num["9"]
    assert strip_distribution_list_footer(SECRETARY_IN_BODY) == SECRETARY_IN_BODY


def test_validation_rejects_addressee_only_sections():
    payload = {
        "statute_name": "The Balochistan Cadet Colleges Act, 2020",
        "sections": [
            {
                "section_number": "1",
                "section_text": "1. The Principal Secretary to Governor Balochistan, Quetta.",
            },
            {
                "section_number": "2",
                "section_text": "2. The Principal Secretary to Chief Minister Balochistan, Quetta.",
            },
            {
                "section_number": "6",
                "section_text": "6. The System Analyst, Balochistan Provincial Assembly.",
            },
        ],
    }
    ok, reason = validate_statute_reconciled_json(payload, raw_text="x" * 200)
    assert not ok
    assert reason == "distribution_list_footer_as_sections"


def test_validation_rejects_a_mixed_statute_that_still_has_an_addressee_section():
    payload = {
        "statute_name": "The Balochistan (Traffic Engineering) Act, 2022",
        "sections": [
            {
                "section_number": "4",
                "section_text": "4. The Government shall establish a Board to carry out the purposes of this Act.",
            },
            {
                "section_number": "5",
                "section_text": "5. The Director General, Public Relations, Balochistan, Quetta.",
            },
        ],
    }
    ok, reason = validate_statute_reconciled_json(payload, raw_text="x" * 200)
    assert not ok
    assert reason == "distribution_list_footer_as_sections"


def test_validation_accepts_an_operative_section_that_mentions_a_secretary():
    payload = {
        "statute_name": "The Balochistan Example Administration Act, 2024",
        "sections": [
            {
                "section_number": "8",
                "section_text": (
                    "8. The Secretary, Government of Balochistan, shall notify the rules "
                    "in the official Gazette within ninety days."
                ),
            }
        ],
    }
    ok, reason = validate_statute_reconciled_json(payload, raw_text="x" * 200)
    assert ok
    assert reason == ""
