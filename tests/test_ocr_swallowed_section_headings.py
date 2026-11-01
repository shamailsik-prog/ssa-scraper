"""Section headings the line-start splitter skips on the 1975 Urban Planning text layer.

senate.gov.pk 1661312733_937.pdf keeps sections 5, 8, 14, 22, 36 and 44 in
the text, but none of them is a clean ``N.`` at the start of a line:

* ``. 5.`` — a stray period sits in front of the number
* ``t.`` then ``Committees,--`` — the digit is one letter, between 7 and 9
* ``i-'Ft+. Other functions … Board.-`` — junk glued to the marginal note
* ``22, Implementation`` — the period was read as a comma, between 21 and 23
* a second ``35.`` and then ``37.`` — the 6 was read as 5
* ``rt4.`` then a marginal note — between 43 and 45

The definitions clause ``12)`` stays inside section 2. A one-number gap with
no heading-shaped line is not filled in.
"""

from __future__ import annotations

from pathlib import Path

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.parsers.statute_parser import split_into_sections
from scraper.statute_quality.section_reextract import (
    REEXTRACT_MEGA_SECTION_CHARS,
    gate_section_reextract,
)

TEXT_LAYER = Path("tests/fixtures/statute_quality/senate_1661312733_937_text_layer.txt")
NWFP = "North-West Frontier Province Urban Planning Act, 1975"
PDF_URL = "https://senate.gov.pk/uploads/documents/1661312733_937.pdf"


def _by_number(sections):
    return {section["section_number"]: section["section_text"] for section in sections}


def test_leading_period_number_line_is_section_5():
    text = """
4. Appointment of members.-The Board shall consist of members appointed by Government for a term of three years.
. 5.
Disqualification of members.-No person shall be or continue to be a member who has been convicted of an offence involving moral turpitude.
6. Appointment of officers.-The Board may appoint such officers and advisers as it considers necessary for the efficient performance of its functions.
"""
    bodies = _by_number(split_into_sections(text, "Example Act, 1975"))
    assert "Disqualification of members" in bodies["5"]
    assert "Disqualification of members" not in bodies["4"]
    assert "Appointment of officers" in bodies["6"]


def test_single_letter_stub_between_seven_and_nine_is_section_8():
    text = """
7. Meetings of the Board.-The meetings of the Board shall be held at such times and places as may be prescribed by the rules.
t.
Committees.--The Board may constitute such financial and technical committees as may be necessary for carrying out the purposes of this Act.
9. Functions of the Board.-The Board shall integrate provincial development planning with the planning of local agencies under this Act.
"""
    bodies = _by_number(split_into_sections(text, "Example Act, 1975"))
    assert "constitute such financial" in bodies["8"]
    assert "constitute such financial" not in bodies["7"]
    assert "integrate provincial" in bodies["9"]


def test_garbled_number_glued_to_marginal_note_is_section_14():
    text = """
13. Provincial plans.-The Board shall prepare a plan or programme for a particular development project of provincial concern when Government so requires.
i-'Ft+. Other functions of the Board.-The Board may provide technical guidance and support for the coordination of urban planning in the Province.
15. Local area authorities.-When the Board is of the view that implementation requires local area planning it may establish a Local Area Authority.
"""
    bodies = _by_number(split_into_sections(text, "Example Act, 1975"))
    assert "Other functions of the Board" in bodies["14"]
    assert "Other functions of the Board" not in bodies["13"]
    assert "Local Area Authority" in bodies["15"]


def test_comma_instead_of_period_is_section_22():
    text = """
21. Adoption of regulations.-A Local Area Authority may adopt Land Development Regulations pursuant to this Chapter when the notification so authorises.
22, Implementation of approved Local Area Plan. The Local Area Authority shall be responsible for implementing the approved Local Area Plan either itself or through other agencies.
23. Modification of delegation.-The Board may by notified order modify the terms of the delegation of functions to the Local Area Authority.
"""
    bodies = _by_number(split_into_sections(text, "Example Act, 1975"))
    assert "implementing the approved" in bodies["22"]
    assert "implementing the approved" not in bodies["21"]
    assert "notified order" in bodies["23"]


def test_repeated_thirty_five_before_thirty_seven_is_section_36():
    text = """
35. Approval of restrictions.-Regulations of land development imposed by another government agency shall be subject to the prior approval of the planning agency.
35. Public participation and review.-The provisions of section 19 relating to Local Area Plans shall apply to the review of Land Development Regulations.
37. Delegation of enforcement.-Government may delegate enforcement functions to another government agency after consultation with the Board.
"""
    bodies = _by_number(split_into_sections(text, "Example Act, 1975"))
    assert "prior approval" in bodies["35"]
    assert "Public participation" in bodies["36"]
    assert "Public participation" not in bodies["35"]
    assert "enforcement functions" in bodies["37"]


def test_letter_prefixed_number_line_between_forty_three_and_forty_five_is_section_44():
    text = """
43. Appeals.-Any person aggrieved by a determination of a planning agency may appeal to the appropriate Appeal Board within thirty days of the notice.
rt4.
Finality of determination.-The determination or order of a planning agency or of an Appeal Board where an appeal has been preferred shall be final.
45. Penalties.-Any person who violates the provisions of Land Development Regulations shall be punished with fine which may extend to one thousand rupees.
"""
    bodies = _by_number(split_into_sections(text, "Example Act, 1975"))
    assert "shall be final" in bodies["44"]
    assert "shall be final" not in bodies["43"]
    assert "punished with fine" in bodies["45"]


def test_one_number_gap_without_a_heading_line_is_not_filled():
    text = """
4. Appointment of members.-The Board shall consist of members appointed by Government for a term of three years under this Act.
The narrative continues and merely discusses later powers without opening another section heading.
6. Appointment of officers.-The Board may appoint such officers and advisers as it considers necessary for its functions under this Act.
"""
    bodies = _by_number(split_into_sections(text, "Example Act, 1975"))
    assert "5" not in bodies
    assert "narrative continues" in bodies["4"]
    assert "appoint such officers" in bodies["6"]


def test_letter_l_for_digit_one_does_not_steal_the_repeal():
    """``5l . Power`` is not section 51. The later ``51. Repeal`` stays."""
    text = """
50. Protection of action taken in good faith.-No suit shall lie against the Board for anything done in good faith under this Act by its officers or advisers.
5l . Power of Government to make rules.-Government may by notification make rules for carrying this Act into effect and for matters incidental thereto.
52. Effect of other laws.-The provisions of this Act shall have effect notwithstanding anything to the contrary contained in any other law for the time being in force.
51. Repeal.-The Town Improvement Act, 1922, in its application to the Province, is hereby repealed.
"""
    bodies = _by_number(split_into_sections(text, "Example Act, 1975"))
    assert "make rules" in bodies["50"]
    assert "Town Improvement" in bodies["51"]
    assert "notwithstanding" in bodies["52"]


def test_nwfp_reextract_recovers_missing_core_sections():
    """Dry re-extract: sections 5, 8, 14, 22, 36 and 44, and the #394 defs gate."""
    extracted = extract_statute_deterministic(
        html=None,
        text=TEXT_LAYER.read_text(),
        source_meta={"source_name": "Senate", "url": PDF_URL, "act_title": NWFP},
    )
    sections = extracted["sections"]
    bodies = _by_number(sections)
    blob = "\n".join(bodies.values())

    assert [str(number) for number in range(1, 53) if str(number) not in bodies] == []
    assert len(sections) == 52
    assert "Disqurlif,cation of members" in bodies["5"]
    assert "Disqurlif" not in bodies["4"]
    assert "Connitteos" in bodies["8"]
    assert "Connitteos" not in bodies["7"]
    assert "Otter functions of the Boarrt" in bodies["14"]
    assert "Otter functions" not in bodies["13"]
    assert "Inplcmentadon of approved" in bodies["22"]
    assert "Inplcmentadon" not in bodies["21"]
    assert "Prrblic prrtlciprtion" in bodies["36"]
    assert "Prrblic" not in bodies["35"]
    assert "Eimtity of determination" in bodies["44"]
    assert "Eimtity" not in bodies["43"]

    assert extracted["field_evidence"].get("bundled_gazette_isolated") == "35 of 1975"
    assert "Rargers" not in blob
    assert "Pakistan Rangers" not in blob
    assert '12) " Board "' in bodies["2"]
    assert len(bodies["2"]) >= REEXTRACT_MEGA_SECTION_CHARS
    assert len(bodies["2"]) < 10_000
    assert bodies["12"].lstrip().startswith("12.")
    assert "Rcgdetion of rlevelopment" in bodies["12"]
    assert "rncans" not in bodies["12"]
    assert "attachto a Spccial" in bodies["33"]
    long_numbers = [number for number, text in bodies.items() if len(text) >= REEXTRACT_MEGA_SECTION_CHARS]
    assert long_numbers == ["2"]
    # ``5l .`` is still inside section 50. The repeal keeps number 51.
    assert "Poryer of Goyertrment" in bodies["50"]
    assert "hereby repealed" in bodies["51"]

    before = [
        {
            "section_number": str(number),
            "section_text": f"{number}. This stored section states the rule and is not a distribution list.",
        }
        for number in range(1, 41)
    ]
    decision = gate_section_reextract(before, sections)
    assert decision.verdict == "PASS"
    assert decision.may_apply is True
    assert decision.may_unquarantine is True
