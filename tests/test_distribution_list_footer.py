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


ONLY_SECTION_ONE = """
4. The Government shall, by notification, establish a Board to carry out this Act.
8. The Board shall manage and control the affairs of the Bureau.
1. The Principal Secretary to Governor Balochistan, Quetta.
"""

ONLY_SECTIONS_FIVE_AND_SIX = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Bureau may make regulations for carrying out the purposes of this Act.
Copy for information to:-
5. The Director General, Public Relations, Balochistan, Quetta.
6. The System Analyst, Balochistan Provincial Assembly.
"""

GLUED_ON_FINAL_SECTION = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Bureau may make regulations for carrying out the purposes of this Act. Copy forwarded to the Principal Secretary to Governor Balochistan, Quetta and the System Analyst, Balochistan Provincial Assembly.
"""


def test_strip_removes_a_single_trailing_addressee_at_section_1():
    stripped = strip_distribution_list_footer(ONLY_SECTION_ONE)
    assert "establish a Board" in stripped
    assert "manage and control the affairs" in stripped
    assert "Principal Secretary" not in stripped
    sections = split_into_sections(ONLY_SECTION_ONE, "The Example Act, 2024")
    blob = _joined(sections)
    assert "establish a Board" in blob
    assert "Principal Secretary" not in blob
    assert "1" not in [s["section_number"] for s in sections]


LONE_DESIGNATIONS = (
    "1. The Chief Secretary, Balochistan, Quetta.",
    "1. The Secretary to the Governor Balochistan, Quetta.",
    "21. The Secretary, Government of Balochistan, Law Department, Quetta.",
)


def test_strip_removes_any_single_trailing_designation():
    for line in LONE_DESIGNATIONS:
        text = (
            "4. The Government shall, by notification, establish a Board to carry out this Act.\n"
            + line
            + "\n"
        )
        stripped = strip_distribution_list_footer(text)
        assert "establish a Board" in stripped
        assert line not in stripped
        assert "Quetta" not in stripped


def test_strip_removes_only_sections_5_and_6():
    stripped = strip_distribution_list_footer(ONLY_SECTIONS_FIVE_AND_SIX)
    assert "make regulations" in stripped
    assert "Director General, Public Relations" not in stripped
    assert "System Analyst" not in stripped
    sections = split_into_sections(ONLY_SECTIONS_FIVE_AND_SIX, "The Example Act, 2024")
    numbers = [s["section_number"] for s in sections]
    assert "4" in numbers
    assert "12" in numbers
    assert "5" not in numbers
    assert "6" not in numbers
    assert "System Analyst" not in _joined(sections)


GLUED_ON_ITS_OWN_TRAILING_LINES = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Bureau may make regulations for carrying out the purposes of this Act.
Copy forwarded to the Principal Secretary to Governor Balochistan, Quetta and the System Analyst, Balochistan Provincial Assembly.
"""

COPY_FORWARDED_INSIDE_AN_OPERATIVE_SENTENCE = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. A copy forwarded to the Secretary shall be kept with the rules the Board may make.
"""


def test_strip_removes_copy_forwarded_glued_onto_the_final_section():
    stripped = strip_distribution_list_footer(GLUED_ON_FINAL_SECTION)
    assert "establish a Board" in stripped
    assert "make regulations" in stripped
    assert "Copy forwarded" not in stripped
    assert "Principal Secretary" not in stripped
    assert "System Analyst" not in stripped
    sections = split_into_sections(GLUED_ON_FINAL_SECTION, "The Example Act, 2024")
    by_num = {s["section_number"]: s["section_text"] for s in sections}
    assert "12" in by_num
    assert "make regulations" in by_num["12"]
    assert "copy forwarded" not in by_num["12"].lower()
    assert "Principal Secretary" not in by_num["12"]
    assert "System Analyst" not in by_num["12"]


def test_strip_removes_copy_forwarded_appended_on_following_lines():
    stripped = strip_distribution_list_footer(GLUED_ON_ITS_OWN_TRAILING_LINES)
    assert "make regulations" in stripped
    assert "Copy forwarded" not in stripped
    assert "Principal Secretary" not in stripped
    assert "System Analyst" not in stripped


def test_copy_forwarded_inside_an_operative_sentence_stays():
    assert (
        strip_distribution_list_footer(COPY_FORWARDED_INSIDE_AN_OPERATIVE_SENTENCE)
        == COPY_FORWARDED_INSIDE_AN_OPERATIVE_SENTENCE
    )


EXTRAORDINARY_ISSUE = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Government must publish the notification in an extra-ordinary issue of the official Gazette.
1. The Principal Secretary to Governor Balochistan, Quetta.
6. The System Analyst, Balochistan Provincial Assembly.
"""


def test_operative_extraordinary_issue_above_addressees_is_kept():
    """A final provision that says must/publish in an extra-ordinary issue is not printing furniture."""
    stripped = strip_distribution_list_footer(EXTRAORDINARY_ISSUE)
    assert "must publish the notification in an extra-ordinary issue" in stripped
    assert "Principal Secretary" not in stripped
    assert "System Analyst" not in stripped
    sections = split_into_sections(EXTRAORDINARY_ISSUE, "The Example Act, 2024")
    by_num = {s["section_number"]: s["section_text"] for s in sections}
    assert "12" in by_num
    assert "extra-ordinary issue" in by_num["12"]
    assert "must publish" in by_num["12"]
    assert "1" not in by_num
    assert "6" not in by_num


# Pilot shapes that still failed after #380/#381. The printing letter wraps, so
# "be sent" / "may please" land on their own lines and were kept as Act text.
FAIL_FINAL_GLUED_COPY_FORWARDED = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Bureau may make regulations for carrying out the purposes of this Act.
(TAHIR SHAH KAKAR)
Secretary.
No.PAB/Legis:V(05)/2022/7727. Dated Quetta, the 08th March, 2022.
A copy is forwarded to the Chief Controller, Government
Printing Press, Balochistan, Quetta for favour of publication in the next issue of Balochistan Gazette. Before final printing, a copy
thereof be sent to this Secretariat for proof reading. Fifty copies of the
Act may please be supplied to this Secretariat for record.
(ABDUL REHMAN)
Special Secretary (Legis:)
No.PAB/Legis: V (18)/2025/ Dated: Quetta, the 05th June, 2025.
A copy is forwarded to the Chief Controller, Government
Printing Press, Balochistan Quetta for favor of publication in Extra –
ordinary issue of Gazette of Balochistan. Before final printing, a copy
thereof be sent to this Secretariat for Proof reading. Fifty copies of the
Act may please be supplied to this Secretariat for record.
No.PAB/Legis:V(05)/2022/7727. Dated Quetta, the 08th March, 2022.
A Copy is forwarded for information and necessary action to:-
1. The Principal Secretary to Governor Balochistan, Quetta.
6. The System Analyst, Balochistan Provincial Assembly.
"""

FAIL_SAME_LINE_FORWARDING = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Bureau may make regulations for carrying out the purposes of this Act. No.PAB/Legis:V(05)/2022/7727. Dated Quetta, the 08th March, 2022. A copy is forwarded to the Chief Controller, Government Printing Press, Balochistan, Quetta for favour of publication in the next issue of Balochistan Gazette. Before final printing, a copy thereof be sent to this Secretariat for proof reading. Fifty copies of the Act may please be supplied to this Secretariat for record.
"""

FAIL_FIFTY_COPIES_ON_OPERATIVE_LINE = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Bureau may make regulations for carrying out the purposes of this Act. Fifty copies of the Act may please be supplied to this Secretariat for record.
"""

FAIL_ADDRESSEE_SECTIONS = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Bureau may make regulations for carrying out the purposes of this Act.
1. The Principal Secretary to Governor Balochistan, Quetta.
2. The Principal Secretary to Chief Minister Balochistan, Quetta.
5. The Director General, Public Relations, Balochistan, Quetta for favour of publication.
6. The System Analyst, Balochistan Provincial Assembly.
Fifty copies of the Act may please be supplied to this Secretariat for record.
"""

FAIL_PRINTING_LETTER_ONLY = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Bureau may make regulations for carrying out the purposes of this Act.
No.PAB/Legis:V(05)/2022/7727. Dated Quetta, the 08th March, 2022.
A copy is forwarded to the Chief Controller, Government Printing Press, Balochistan, Quetta for favour of publication in the next issue of Balochistan Gazette.
Before final printing, a copy thereof be sent to this Secretariat for proof reading.
After proof reading fifty copies of the said Act may please be provided to this Secretariat for record.
"""

MED1_THEN_FIFTY_COPIES = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Government must publish the notification in an extra-ordinary issue of the official Gazette. Fifty copies of the Act may please be supplied to this Secretariat for record.
1. The Principal Secretary to Governor Balochistan, Quetta.
"""

_FORWARDING_PHRASES = (
    "No.PAB",
    "Chief Controller",
    "Printing Press",
    "Fifty copies",
    "fifty copies",
    "proof reading",
    "Proof reading",
    "may please",
    "Principal Secretary",
    "System Analyst",
    "Copy is forwarded",
    "copy is forwarded",
    "Copy forwarded",
)


def _assert_forwarding_gone(text: str) -> None:
    lowered = text.lower()
    for phrase in _FORWARDING_PHRASES:
        assert phrase.lower() not in lowered, phrase


def test_fail_final_glued_copy_forwarded_strips_wrapped_printing_letter():
    """Wrapped Chief Controller / Fifty copies lines must leave the last operative section."""
    stripped = strip_distribution_list_footer(FAIL_FINAL_GLUED_COPY_FORWARDED)
    assert "make regulations" in stripped
    assert "establish a Board" in stripped
    _assert_forwarding_gone(stripped)
    sections = split_into_sections(FAIL_FINAL_GLUED_COPY_FORWARDED, "The Example Act, 2022")
    blob = _joined(sections)
    assert "make regulations" in blob
    _assert_forwarding_gone(blob)
    by_num = {s["section_number"]: s["section_text"] for s in sections}
    assert "12" in by_num
    assert "may make regulations" in by_num["12"]
    assert "1" not in by_num
    assert "6" not in by_num


def test_fail_final_glued_copy_forwarded_strips_same_line_reference_and_copy_count():
    for text in (FAIL_SAME_LINE_FORWARDING, FAIL_FIFTY_COPIES_ON_OPERATIVE_LINE):
        stripped = strip_distribution_list_footer(text)
        assert "make regulations" in stripped
        _assert_forwarding_gone(stripped)
        sections = split_into_sections(text, "The Example Act, 2022")
        by_num = {s["section_number"]: s["section_text"] for s in sections}
        assert "12" in by_num
        assert "make regulations" in by_num["12"]
        _assert_forwarding_gone(by_num["12"])


def test_fail_addressee_sections_when_copy_count_follows_the_list():
    """A trailing copy-count line must not keep the numbered addressees as sections."""
    stripped = strip_distribution_list_footer(FAIL_ADDRESSEE_SECTIONS)
    assert "make regulations" in stripped
    _assert_forwarding_gone(stripped)
    sections = split_into_sections(FAIL_ADDRESSEE_SECTIONS, "The Example Act, 2022")
    blob = _joined(sections)
    assert "establish a Board" in blob
    assert "make regulations" in blob
    _assert_forwarding_gone(blob)
    numbers = [s["section_number"] for s in sections]
    assert "4" in numbers
    assert "12" in numbers
    assert "1" not in numbers
    assert "2" not in numbers
    assert "5" not in numbers
    assert "6" not in numbers
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "The Example Act, 2022", "sections": sections},
        raw_text="x" * 200,
    )
    assert ok, reason


def test_printing_letter_without_addressees_is_stripped_from_the_last_section():
    stripped = strip_distribution_list_footer(FAIL_PRINTING_LETTER_ONLY)
    assert "make regulations" in stripped
    _assert_forwarding_gone(stripped)
    sections = split_into_sections(FAIL_PRINTING_LETTER_ONLY, "The Example Act, 2022")
    by_num = {s["section_number"]: s["section_text"] for s in sections}
    assert "12" in by_num
    _assert_forwarding_gone(_joined(sections))


MED1_WRAPPED = """
4. The Government shall, by notification, establish a Board to carry out this Act.
12. The Government must publish the notification in an extra-
ordinary issue of the official Gazette.
1. The Principal Secretary to Governor Balochistan, Quetta.
6. The System Analyst, Balochistan Provincial Assembly.
"""


def test_wrapped_operative_extra_ordinary_issue_is_kept():
    """A line break inside 'extra-ordinary issue' is still the final provision."""
    stripped = strip_distribution_list_footer(MED1_WRAPPED)
    assert "must publish the notification in an extra-" in stripped
    assert "ordinary issue of the official Gazette" in stripped
    assert "Principal Secretary" not in stripped
    assert "System Analyst" not in stripped
    sections = split_into_sections(MED1_WRAPPED, "The Example Act, 2024")
    blob = _joined(sections)
    assert "must publish the notification" in blob
    assert "ordinary issue of the official Gazette" in blob
    assert "Principal Secretary" not in blob
    numbers = [s["section_number"] for s in sections]
    assert "1" not in numbers
    assert "6" not in numbers


def test_operative_extra_ordinary_line_keeps_text_when_fifty_copies_follows():
    stripped = strip_distribution_list_footer(MED1_THEN_FIFTY_COPIES)
    assert "must publish the notification in an extra-ordinary issue" in stripped
    _assert_forwarding_gone(stripped)
    sections = split_into_sections(MED1_THEN_FIFTY_COPIES, "The Example Act, 2024")
    by_num = {s["section_number"]: s["section_text"] for s in sections}
    assert "must publish the notification in an extra-ordinary issue" in by_num["12"]
    assert "Fifty copies" not in by_num["12"]
    assert "1" not in by_num


def test_validation_rejects_a_printing_forwarding_block_without_an_addressee():
    letter = (
        "No.PAB/Legis:V(05)/2022/7727. Dated Quetta, the 08th March, 2022.\n"
        "A copy is forwarded to the Chief Controller, Government Printing Press, "
        "Balochistan, Quetta for favour of publication.\n"
        "Before final printing, a copy thereof be sent to this Secretariat for proof reading.\n"
        "Fifty copies of the Act may please be supplied to this Secretariat for record."
    )
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "The Example Act, 2022",
            "sections": [
                {
                    "section_number": "12",
                    "section_text": "12. The Bureau may make regulations for carrying out the purposes of this Act.",
                },
                {"section_number": "6", "section_text": letter},
            ],
        },
        raw_text="x" * 200,
    )
    assert not ok
    assert reason == "distribution_list_footer_as_sections"
    kept = {
        "statute_name": "The Example Act, 2024",
        "sections": [
            {
                "section_number": "12",
                "section_text": (
                    "12. The Government must publish the notification in an "
                    "extra-ordinary issue of the official Gazette."
                ),
            }
        ],
    }
    ok, reason = validate_statute_reconciled_json(kept, raw_text="x" * 200)
    assert ok
    assert reason == ""


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


def test_validation_rejects_a_designation_only_section_at_any_number():
    real = {
        "section_number": "4",
        "section_text": "4. The Government shall establish a Board to carry out the purposes of this Act.",
    }
    cases = (
        ("1", "1. The Principal Secretary to Governor Balochistan, Quetta."),
        ("5", "5. The Director General, Public Relations, Balochistan, Quetta."),
        ("6", "6. The System Analyst, Balochistan Provincial Assembly."),
        ("21", "21. The Secretary, Government of Balochistan, Law Department, Quetta."),
    )
    for number, text in cases:
        ok, reason = validate_statute_reconciled_json(
            {
                "statute_name": "The Example Act, 2024",
                "sections": [real, {"section_number": number, "section_text": text}],
            },
            raw_text="x" * 200,
        )
        assert not ok, number
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
