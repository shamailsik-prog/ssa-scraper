from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.parsers.statute_parser import split_into_sections


def test_split_into_sections_rejects_footnote_annotation_phantoms():
    text = """
1. Preliminary.- This Act extends to the whole Province and applies to all public channels.
2. Definitions.- In this Act, unless there is anything repugnant in the subject or context, the following expressions shall have the meanings assigned.
279. Substituted by Gazette of Pakistan Extraordinary, Part I, dated 12 June 1975, for sections 271, 272 and 279.
"""

    sections = split_into_sections(text, "Example Irrigation Act, 2000")
    section_numbers = [section["section_number"] for section in sections]

    assert "1" in section_numbers
    assert "2" in section_numbers
    assert "279" not in section_numbers


def test_split_into_sections_applies_canal_drainage_section_cap():
    text = """
1. Short title and extent.- This Act may be called the Canal and Drainage Act and extends to the relevant area.
75. Power to make rules.- The Government may frame rules for carrying out the purposes of this Act.
279. Supplemental transfer powers.- Any canal officer may transfer watercourses subject to directions issued by the Government.
542. West Pakistan Gazette Extraordinary, dated 02 February 1963.
"""

    sections = split_into_sections(text, "Canal and Drainage Act, 1873")
    section_numbers = [section["section_number"] for section in sections]

    assert "1" in section_numbers
    assert "75" in section_numbers
    assert "279" not in section_numbers
    assert "542" not in section_numbers


def test_extract_statute_uses_official_title_when_name_is_short_title_clause():
    short_title_clause = "This Act may be called the Central Depositories Act, 1997."
    text = f"""
1. Short title.- {short_title_clause}
2. Definitions.- In this Act, the context otherwise requires that capitalized terms have the meanings assigned in this section.
"""

    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "PakistanCode",
            "statute_name": short_title_clause,
            "act_title": "Central Depositories Act, 1997",
        },
    )

    assert extracted["statute_name"] == "Central Depositories Act, 1997"
