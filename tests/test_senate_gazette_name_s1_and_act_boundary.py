"""Stored short title versus section 1, and a later act glued into a section tail.

MAIN 959ea718 / FREE-SITES eec29963 are stored as The Short Titles Act, 1973
while section 1 says the Service Tribunals Act, 1973. National Registration
(Amendment) 1976 and House Building Finance Corporation (Amendment) 1975 keep
the next gazette header inside a section because that header is not at the
start of a line and the next act's own section 1 is missing.
"""

from __future__ import annotations

from scraper.document_validation import validate_statute_reconciled_json
from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.statute_quality.bundled_gazette import (
    REASON_BLEED,
    VERDICT_MISMATCH,
    classify_bundled_gazette,
)
from scraper.statute_quality.senate_integrity import (
    ACTION_NONE,
    ACTION_QUARANTINE,
    VERDICT_MATCH,
    act_segments,
    classify_senate_identity,
)

SHORT_TITLES = "The Short Titles Act, 1973"
SERVICE_TRIBUNALS = "The Service Tribunals Act, 1973"
NATIONAL_REGISTRATION = "The National Registration (Amendment) Act, 1976"
HOUSE_BUILDING = "The House Building Finance Corporation (Amendment) Act, 1975"

SERVICE_S1 = (
    "1. Short title, extent and commencement.—(1) This Act may be called "
    "the Service Tribunals Act, 1973."
)
SERVICE_S2 = (
    "In this Act, unless there is anything repugnant in the subject or context, "
    "Tribunal means a Service Tribunal established under this Act to hear appeals "
    "of civil servants."
)

# Two acts share the PDF. The stored row kept only the Service Tribunals
# sections, under a name taken from the short-title heading.
SHORT_TITLES_BUNDLE = f"""
THE GAZETTE OF PAKISTAN
EXTRAORDINARY

ACT No. LXVII of 1973
An Act further to amend the Life Insurance (Nationalisation) Act, 1972.
1. Short title.—This Act may be called the Life Insurance (Nationalisation) (Amendment) Act, 1973.
2. Amendment of section 4.—In the Life Insurance (Nationalisation) Act, 1972, for the word insurer the word corporation shall be substituted throughout the vesting schedule.

ACT No. LXX of 1973
An Act to provide for the establishment of Service Tribunals.
{SERVICE_S1}
2. Definitions.—{SERVICE_S2}
"""

REGISTRATION_S2 = (
    "In the National Registration Act, 1973, in section 2, for the word citizen "
    "the word resident shall be substituted throughout the registration roll"
)
HOUSE_S2 = (
    "In the House Building Finance Corporation Act, 1952, in section 10, the "
    "Corporation shall keep a register of financed houses and shall publish that "
    "register each year in the official Gazette"
)

# The next ACT No. is glued to the previous section. The next act has no section 1.
GLUED_AMENDMENTS = f"""
ACT No. XX of 1976
An Act further to amend the National Registration Act, 1973.
1. Short title.—This Act may be called the National Registration (Amendment) Act, 1976.
2. Amendment of section 2.—{REGISTRATION_S2}. ACT No. XLII of 1975
An Act further to amend the House Building Finance Corporation Act, 1952.
2. Amendment of section 10.—{HOUSE_S2}.
"""

GLUED_ONE_LINE = f"""
ACT No. XX of 1976
An Act further to amend the National Registration Act, 1973.
1. Short title.—This Act may be called the National Registration (Amendment) Act, 1976.
2. Amendment of section 2.—{REGISTRATION_S2}. ACT No. XLII of 1975 An Act further to amend the House Building Finance Corporation Act, 1952. 2. Amendment of section 10.—{HOUSE_S2}.
"""

CITATION_INSIDE_SECTION = """
ACT No. XII of 1980
An Act further to amend the Service Tribunals Act, 1973.
1. Short title.—This Act may be called the Service Tribunals (Amendment) Act, 1980.
2. Amendment of section 4.—In the Service Tribunals Act, 1973, after the appeal period, the following shall be read with Act No. LXX of 1973.- In the Service Tribunals Act, 1973, in section 4, for the word thirty the word sixty shall be substituted and the tribunal shall record that substitution.
5. Savings.—Every appeal already filed under the unamended section shall continue and shall be decided under the law as it stood before this amendment came into force.
"""


def _section(number: str, text: str) -> dict:
    return {"section_number": number, "section_text": text}


def test_short_titles_name_disagrees_with_service_tribunals_section_1():
    """The heading 'Short title' is not the instrument section 1 names."""
    result = classify_senate_identity(
        stored_name=SHORT_TITLES,
        year_enacted=1973,
        source_url="https://senate.gov.pk/uploads/documents/1660540467_508.pdf",
        sections=[_section("1", SERVICE_S1), _section("2", SERVICE_S2)],
        pdf_text=SHORT_TITLES_BUNDLE,
        pdf_available=True,
    )
    assert result.verdict == "MISMATCH"
    assert result.action == ACTION_QUARANTINE
    assert result.reason == "name_s1_mismatch"
    assert result.needs_reextract is False
    assert "name_s1_mismatch" in result.signals


def test_called_as_short_title_is_the_same_mismatch():
    called_as = (
        "1. Short title.—This Act may be called as the Service Tribunals Act, 1973."
    )
    result = classify_senate_identity(
        stored_name=SHORT_TITLES,
        year_enacted=1973,
        source_url="https://senate.gov.pk/uploads/documents/1660540467_508.pdf",
        sections=[_section("1", called_as), _section("2", SERVICE_S2)],
        pdf_text=SHORT_TITLES_BUNDLE.replace(SERVICE_S1, called_as),
        pdf_available=True,
    )
    assert result.reason == "name_s1_mismatch"
    assert "name_s1_mismatch" in result.signals


def test_agreeing_section_1_on_a_bundled_gazette_stays_match():
    result = classify_senate_identity(
        stored_name=SERVICE_TRIBUNALS,
        year_enacted=1973,
        source_url="https://senate.gov.pk/uploads/documents/1660540467_508.pdf|LXX of 1973",
        sections=[_section("1", SERVICE_S1), _section("2", SERVICE_S2)],
        pdf_text=SHORT_TITLES_BUNDLE,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.reason is None
    assert "name_s1_mismatch" not in result.signals


def test_ocr_near_section_1_is_not_a_name_mismatch():
    ocr = (
        "1. Short title.—This Act may be called the Servlce Tribunals Act, 1973."
    )
    result = classify_senate_identity(
        stored_name=SERVICE_TRIBUNALS,
        year_enacted=1973,
        source_url="https://senate.gov.pk/uploads/documents/1660540467_508.pdf|LXX of 1973",
        sections=[_section("1", ocr)],
        pdf_text=SHORT_TITLES_BUNDLE,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MATCH
    assert "name_s1_mismatch" not in result.signals


def test_validation_rejects_short_titles_against_service_tribunals_section_1():
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": SHORT_TITLES,
            "sections": [
                _section("1", SERVICE_S1),
                _section("2", SERVICE_S2),
            ],
        },
        raw_text=SHORT_TITLES_BUNDLE,
    )
    assert not ok
    assert reason == "name_s1_mismatch"


def test_validation_accepts_section_1_that_names_the_stored_act():
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": SERVICE_TRIBUNALS,
            "sections": [
                _section("1", SERVICE_S1),
                _section("2", SERVICE_S2),
            ],
        },
        raw_text=SHORT_TITLES_BUNDLE,
    )
    assert ok
    assert reason == ""


def test_midsection_act_header_is_a_boundary_without_a_real_section_1():
    segments = act_segments(GLUED_AMENDMENTS)
    assert len(segments) == 2
    assert segments[0].act_number == (20, 1976)
    assert segments[1].act_number == (42, 1975)
    assert "financed houses" not in segments[0].text
    assert "registration roll" not in segments[1].text
    assert "financed houses" in segments[1].text


def test_same_line_act_header_is_a_boundary():
    segments = act_segments(GLUED_ONE_LINE)
    assert len(segments) == 2
    assert "financed houses" not in segments[0].text
    assert "registration roll" not in segments[1].text


def test_national_registration_extract_drops_the_glued_house_building_act():
    extracted = extract_statute_deterministic(
        html=None,
        text=GLUED_AMENDMENTS,
        source_meta={
            "source_name": "Senate",
            "url": "https://senate.gov.pk/uploads/documents/national-registration-1976.pdf|XX of 1976",
            "act_title": NATIONAL_REGISTRATION,
        },
    )
    blob = "\n".join(section.get("section_text") or "" for section in extracted["sections"])
    assert "registration roll" in blob
    assert "financed houses" not in blob
    assert "ACT No. XLII" not in blob
    assert "House Building" not in blob
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]
    assert extracted["sections"]


def test_house_building_extract_starts_at_the_glued_header_when_section_1_is_missing():
    extracted = extract_statute_deterministic(
        html=None,
        text=GLUED_AMENDMENTS,
        source_meta={
            "source_name": "Senate",
            "url": "https://senate.gov.pk/uploads/documents/national-registration-1976.pdf|XLII of 1975",
            "act_title": HOUSE_BUILDING,
        },
    )
    blob = "\n".join(section.get("section_text") or "" for section in extracted["sections"])
    assert "financed houses" in blob
    assert "registration roll" not in blob
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]


def test_glued_tail_sections_are_bundled_gazette_act_bleed():
    verdict = classify_bundled_gazette(
        stored_name=NATIONAL_REGISTRATION,
        source_url="https://senate.gov.pk/uploads/documents/national-registration-1976.pdf|XX of 1976",
        sections=[
            _section("1", "This Act may be called the National Registration (Amendment) Act, 1976."),
            _section("2", f"{REGISTRATION_S2}. ACT No. XLII of 1975 An Act further to amend the House Building Finance Corporation Act, 1952. {HOUSE_S2}."),
        ],
        pdf_text=GLUED_ONE_LINE,
        pdf_available=True,
    )
    assert verdict.verdict == VERDICT_MISMATCH
    assert verdict.reason == REASON_BLEED
    assert "spans_acts" in verdict.signals


def test_act_number_citation_inside_a_section_is_not_a_new_act():
    segments = act_segments(CITATION_INSIDE_SECTION)
    assert len(segments) == 1
    assert "already filed under the unamended section" in segments[0].text
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "The Service Tribunals (Amendment) Act, 1980",
            "sections": [
                _section(
                    "1",
                    "1. Short title.—This Act may be called the Service Tribunals (Amendment) Act, 1980.",
                ),
                _section(
                    "2",
                    "In the Service Tribunals Act, 1973, after the appeal period, the following shall be read with Act No. LXX of 1973.- In the Service Tribunals Act, 1973, in section 4, for the word thirty the word sixty shall be substituted.",
                ),
                _section(
                    "5",
                    "Every appeal already filed under the unamended section shall continue and shall be decided under the law as it stood before this amendment came into force.",
                ),
            ],
        },
        raw_text=CITATION_INSIDE_SECTION,
    )
    assert ok
    assert reason == ""


def test_validation_rejects_a_section_tail_that_still_contains_the_next_act():
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": NATIONAL_REGISTRATION,
            "sections": [
                _section(
                    "1",
                    "1. Short title.—This Act may be called the National Registration (Amendment) Act, 1976.",
                ),
                _section(
                    "2",
                    f"{REGISTRATION_S2}. ACT No. XLII of 1975 An Act further to amend the House Building Finance Corporation Act, 1952. {HOUSE_S2}.",
                ),
            ],
        },
        raw_text=GLUED_ONE_LINE,
    )
    assert not ok
    assert reason == "bundled_gazette_act_bleed"
