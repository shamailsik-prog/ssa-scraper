from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.parsers.statute_parser import (
    detect_statute_name,
    listed_title_supported_by_text,
    looks_like_fragment_name,
    prefer_official_statute_title,
    split_into_sections,
)


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


def test_looks_like_fragment_name_catches_url_and_filename_leftovers():
    assert looks_like_fragment_name("UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tqaw%3D%3D")
    assert looks_like_fragment_name("Administrator Act 2026")
    assert looks_like_fragment_name("administrator-act-2026.pdf")
    assert looks_like_fragment_name("/pdffiles/foo.pdf")
    assert not looks_like_fragment_name("Pakistan Penal Code, 1860")
    assert not looks_like_fragment_name("Defense Forces of Pakistan Act, 2026")


def test_detect_statute_name_rejects_pdffiles_slug_fallback():
    name = detect_statute_name(
        "1. Short title.- This Act applies throughout Pakistan and has no printed long title on this page.",
        "https://pakistancode.gov.pk/pdffiles/administrator-act-2026.pdf",
    )
    assert name == "Unknown Statute"
    assert not looks_like_fragment_name(name)


def test_prefer_official_title_wins_over_url_fragment():
    assert prefer_official_statute_title(
        "Administrator Act 2026",
        "Defense Forces of Pakistan Act, 2026",
    ) == "Defense Forces of Pakistan Act, 2026"
    assert prefer_official_statute_title(
        "Unknown Statute",
        "Central Depositories Act, 1997",
    ) == "Central Depositories Act, 1997"
    assert prefer_official_statute_title(
        "Alpha Fisheries Act, 2026",
        "Beta Forestry Act, 2026",
    ) == "Beta Forestry Act, 2026"


def test_listed_title_supported_by_text_requires_real_overlap():
    body = "1. Short title.- This Act may be called the Defense Forces of Pakistan Act, 2026."
    assert listed_title_supported_by_text("Defense Forces of Pakistan Act, 2026", body)
    assert not listed_title_supported_by_text("Unrelated Fisheries Ordinance, 2024", body)


def test_extract_statute_uses_listing_title_instead_of_pdf_filename():
    text = """
1. Short title and extent.- This Act shall be called the Defense Forces of Pakistan Act, 2026 and extends to the whole of Pakistan.
2. Definitions.- In this Act, unless there is anything repugnant in the subject or context, force means the armed forces.
"""
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "PakistanCode",
            "url": "https://pakistancode.gov.pk/pdffiles/administrator-act-2026.pdf",
            "act_title": "Defense Forces of Pakistan Act, 2026",
        },
    )
    assert extracted["statute_name"] == "Defense Forces of Pakistan Act, 2026"
    assert "listed_title_absent" not in (extracted.get("field_evidence") or {})


def test_extract_statute_flags_listed_title_absent_on_shared_junk_pdf():
    text = "Scanned placeholder page with no operative sections or act heading."
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "PakistanCode",
            "url": "https://pakistancode.gov.pk/pdffiles/shared.pdf",
            "act_title": "Defense Forces of Pakistan Act, 2026",
        },
    )
    assert extracted["statute_name"] == "Defense Forces of Pakistan Act, 2026"
    assert (extracted.get("field_evidence") or {}).get("listed_title_absent")
