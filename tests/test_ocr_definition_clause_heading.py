"""OCR ``(`` read as ``1`` inside a definitions section.

senate.gov.pk 1661312733_937.pdf prints definition clause ``(2)`` as ``12)``.
The line-start splitter stored that as section 12 and dropped the real
``12. Regulation`` heading. The clause stays inside section 2. A later
``12.`` heading is still a section. A ``12)`` heading outside an open
definitions block is still a section.
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

OCR_DEFINITIONS = """
1. Short title, extent and commencement.—(1) This Act may be called the Example Urban Planning Act, 1975.
(2) It extends to the whole of the Province and comes into force at once.
2. Definitions.—In this Act, unless there is anything repugnant in the subject or context,—
(1) "agriculture" includes farming, horticulture and the use of land for crops;
12) "Board" means the Provincial Urban Development Board established under section 3;
(3) "development" includes the subjects embraced by the operative development plan;
3. Establishment of the Board.—(1) The Government shall establish a Board to perform the functions assigned to it under this Act.
12. Regulation of development.—(1) The Board may regulate development in a provincial control area and shall notify the decision.
"""

PAREN_SECTION_HEADINGS = """
1) Short title. This Act may be called the Example Penalties Act, 1975 and it extends to the whole Province.
2) Powers. The authority shall publish the rules in the official Gazette within ninety days of commencement.
12) Penalties. Whoever contravenes this Act shall be punished with fine which may extend to ten thousand rupees.
"""


def _by_number(sections):
    return {section["section_number"]: section["section_text"] for section in sections}


def test_ocr_paren_inside_definitions_is_not_section_12():
    sections = split_into_sections(OCR_DEFINITIONS, "Example Urban Planning Act, 1975")
    bodies = _by_number(sections)
    assert [section["section_number"] for section in sections] == ["1", "2", "3", "12"]
    assert '12) "Board" means the Provincial Urban Development Board' in bodies["2"]
    assert "Regulation of development" in bodies["12"]
    assert "Provincial Urban Development Board" not in bodies["12"]


LATER_SUBSECTION = """
1. Short title.—This Act may be called the Example Act, 1975 and it extends to the Province.
2. Definitions.—In this Act, unless the context otherwise requires,—
(1) "Board" means the Board established under this Act.
12. Regulation of development.—The Board may regulate development in a control area and shall notify that decision.
33. Special permits.—(1) The planning agency may issue a permit for a departure from the general requirements of this Act.
12) A planning agency may attach conditions to a permit issued under this section.
(3) The regulations shall prescribe the procedure for an application under this section.
34. General permits.—The regulations shall prescribe a procedure for a general permit under this Act.
"""


def test_later_paren_ocr_subsection_stays_with_its_section():
    """``(2)`` read as ``12)`` after a real ``12.`` stays in that later section."""
    sections = split_into_sections(LATER_SUBSECTION, "Example Act, 1975")
    bodies = _by_number(sections)
    assert "regulate development" in bodies["12"]
    assert "attach conditions" not in bodies["12"]
    assert "attach conditions" in bodies["33"]
    assert "procedure for an application" in bodies["33"]
    assert "general permit" in bodies["34"]


def test_paren_section_12_outside_definitions_is_a_heading():
    sections = split_into_sections(PAREN_SECTION_HEADINGS, "Example Penalties Act, 1975")
    bodies = _by_number(sections)
    assert bodies["12"].lstrip().startswith("12)")
    assert "punished with fine" in bodies["12"]
    assert "12" in bodies


def test_nwfp_reextract_passes_mega_gate_without_a_fake_section_12():
    """Dry re-extract of the senate text layer: no Rangers bleed, no mega section 12."""
    extracted = extract_statute_deterministic(
        html=None,
        text=TEXT_LAYER.read_text(),
        source_meta={"source_name": "Senate", "url": PDF_URL, "act_title": NWFP},
    )
    sections = extracted["sections"]
    bodies = _by_number(sections)
    blob = "\n".join(bodies.values())

    assert extracted["field_evidence"].get("bundled_gazette_isolated") == "35 of 1975"
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]
    assert "Rargers" not in blob
    assert "Pakistan Rangers" not in blob
    assert "Public Servicc Commission" not in blob

    assert '12) " Board "' in bodies["2"]
    assert "rncans" in bodies["2"]
    assert len(bodies["2"]) >= REEXTRACT_MEGA_SECTION_CHARS
    assert bodies["12"].lstrip().startswith("12.")
    assert "Rcgdetion of rlevelopment" in bodies["12"]
    assert len(bodies["12"]) < REEXTRACT_MEGA_SECTION_CHARS
    assert "rncans" not in bodies["12"]
    assert "attachto a Spccial" in bodies["33"]
    assert len(bodies["33"]) < REEXTRACT_MEGA_SECTION_CHARS
    long_numbers = [number for number, text in bodies.items() if len(text) >= REEXTRACT_MEGA_SECTION_CHARS]
    assert long_numbers == ["2"]

    before = [
        {
            "section_number": str(number),
            "section_text": f"{number}. This stored section states the rule and is not a distribution list.",
        }
        for number in range(1, 41)
    ]
    decision = gate_section_reextract(before, sections)
    assert decision.verdict == "PASS"
    assert decision.reason == ""
    assert decision.may_apply is True
