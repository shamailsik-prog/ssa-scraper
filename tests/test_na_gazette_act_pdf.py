from __future__ import annotations

from pathlib import Path

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.fetchers import statute_staging_identity_url
from scraper.parsers.assembly_pdf_sections import filter_assembly_pdf_sections, normalize_assembly_gazette_pdf_text
from scraper.parsers.statute_parser import detect_statute_name, split_into_sections

FIXTURE = Path(__file__).resolve().parent / "fixtures_statutes" / "na_pidama_act_2026_gazette_excerpt.txt"
PDF_URL = "https://www.na.gov.pk/uploads/documents/6adee709285af_925.pdf"
OFFICIAL_TITLE = (
    "The Pakistan Infrastructure Development and Assets Management Authority Act, 2026 (Act No. LII of 2026)"
)


def _excerpt() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_normalize_assembly_gazette_pdf_text_fixes_ocr_section_markers():
    raw = _excerpt()
    normalized = normalize_assembly_gazette_pdf_text(raw)
    assert "l.\nShort title" not in normalized
    assert "1.\nShort title" in normalized or "1. Short title" in normalized
    assert "3l.\nIndemnity" not in normalized
    assert "31. Indemnity" in normalized
    assert "I. Authority" not in normalized
    assert "11. Authority" in normalized


def test_split_captures_short_title_and_gazette_interrupted_sections():
    text = normalize_assembly_gazette_pdf_text(_excerpt())
    sections = filter_assembly_pdf_sections(split_into_sections(text, OFFICIAL_TITLE))
    numbers = {s["section_number"] for s in sections}
    assert "1" in numbers
    assert "11" in numbers
    assert "18" in numbers
    assert "29" in numbers
    assert "31" in numbers
    assert "1898" not in numbers
    assert "0" not in numbers


def test_detect_statute_name_prefers_act_title_not_short_title_clause():
    text = normalize_assembly_gazette_pdf_text(_excerpt())
    detected = detect_statute_name(text, PDF_URL)
    assert "shall be called" not in detected.lower()
    assert "Infrastructure" in detected or detected.endswith("2026")
    assert "Act" in detected


def test_extract_statute_uses_listing_title_and_finds_core_sections():
    text = normalize_assembly_gazette_pdf_text(_excerpt())
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "NationalAssembly",
            "url": PDF_URL,
            "act_title": OFFICIAL_TITLE,
        },
    )
    assert extracted["statute_name"] == OFFICIAL_TITLE
    numbers = {s["section_number"] for s in extracted["sections"]}
    assert {"1", "11", "18", "29", "31"}.issubset(numbers)


def test_statute_staging_identity_url_rejects_listing_chrome_suffix():
    polluted = "|Share Print Acts of Parliament Filter by"
    assert statute_staging_identity_url(PDF_URL, {"act_title": polluted}) is None
    assert statute_staging_identity_url(PDF_URL, {"act_title": OFFICIAL_TITLE}) == f"{PDF_URL}|{OFFICIAL_TITLE[:200]}"
