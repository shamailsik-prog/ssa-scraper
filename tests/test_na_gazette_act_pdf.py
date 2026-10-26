from __future__ import annotations

from pathlib import Path

import pytest

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.fetchers import statute_staging_identity_url
from scraper.parsers.assembly_pdf_sections import (
    contains_gazette_page_header_fragment,
    filter_assembly_pdf_sections,
    normalize_assembly_gazette_pdf_text,
    strip_gazette_page_break_lines,
)
from scraper.parsers.statute_parser import _repair_act_title_ocr, detect_statute_name, split_into_sections

FIXTURE = Path(__file__).resolve().parent / "fixtures_statutes" / "na_pidama_act_2026_gazette_excerpt.txt"
PDF_URL = "https://www.na.gov.pk/uploads/documents/6adee709285af_925.pdf"
OFFICIAL_TITLE = (
    "The Pakistan Infrastructure Development and Assets Management Authority Act, 2026 (Act No. LII of 2026)"
)
DETECTED_TITLE = "Pakistan Infrastructure Development and Assets Management Authority Act, 2026"
LISTING_CHROME_TITLE = "|Share Print Acts of Parliament Filter by"


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


def test_detect_statute_name_exact_title_without_listing_metadata():
    text = normalize_assembly_gazette_pdf_text(_excerpt())
    detected = detect_statute_name(text, PDF_URL)
    assert detected == DETECTED_TITLE


def test_extract_statute_exact_title_without_act_title():
    text = normalize_assembly_gazette_pdf_text(_excerpt())
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "NationalAssembly",
            "url": PDF_URL,
        },
    )
    assert extracted["statute_name"] == DETECTED_TITLE


def test_extract_statute_exact_title_with_chrome_only_act_title():
    text = normalize_assembly_gazette_pdf_text(_excerpt())
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "NationalAssembly",
            "url": PDF_URL,
            "act_title": LISTING_CHROME_TITLE,
        },
    )
    assert extracted["statute_name"] == DETECTED_TITLE


def test_extract_statute_uses_listing_title_when_official_title_provided():
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


def test_strip_gazette_page_break_preserves_substantive_gazette_citation():
    body = (
        "12. Publication.- (2) Every rule made under this Act shall be published "
        "in the Gazette of Pakistan and shall take effect on such date as may be specified."
    )
    cleaned = strip_gazette_page_break_lines(body)
    assert "published in the Gazette of Pakistan" in cleaned


def test_strip_gazette_page_break_removes_header_shaped_lines():
    body = (
        "18. Cognizance.- No court shall take cognizance.\n"
        "12,2026\n"
        "920 THE GAZETTE OF PAKISTAN, EXTRA., OCT. [PART I\n"
        "918 THE CAZETTE OF PAKISTAN, EXTRA., OCT. 12, 2026 [PART I\n"
        "[PART\n"
        "trial.(1)"
    )
    cleaned = strip_gazette_page_break_lines(body)
    assert "published in the Gazette" not in cleaned or "Cognizance" in cleaned
    assert not contains_gazette_page_header_fragment(cleaned)
    assert "trial.(1)" in cleaned


def test_repair_act_title_ocr_does_not_mutate_valid_preamble_words():
    preamble = (
        "AN ACT to provide for establishment of Pakistan Infrastructure Development. "
        "The Authority and the Province shall exercise powers under this Act."
    )
    title = "Pakistan Infrastructure Development Authority Act, 2026"
    repaired = _repair_act_title_ocr(title, preamble)
    assert "Authority" in repaired
    assert "Authorities" not in repaired
    province_title = "Province Development Regulation Act, 2024"
    province_repaired = _repair_act_title_ocr(province_title, preamble)
    assert "Province" in province_repaired
    assert "Provinces" not in province_repaired
    assert " provide" not in province_repaired

    plural_preamble = preamble + " The Provinces may coordinate."
    assert "Province" in _repair_act_title_ocr(province_title, plural_preamble)
    assert "Provinces" not in _repair_act_title_ocr(province_title, plural_preamble)


def test_repair_act_title_ocr_fixes_close_gazette_typos_only():
    preamble = "AN ACT to provide for establishment of Pakistan Infrastructure Development and Assets"
    repaired = _repair_act_title_ocr(
        "Pakistan Inliastructure Development and Assets Management Authority Act, 2026",
        preamble,
    )
    assert "Infrastructure" in repaired
    assert "Inliastructure" not in repaired


def test_gazette_page_headers_stripped_from_stored_section_bodies():
    text = normalize_assembly_gazette_pdf_text(_excerpt())
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={"source_name": "NationalAssembly", "url": PDF_URL},
    )
    by_num = {s["section_number"]: s for s in extracted["sections"]}
    assert "THE GAZETTE OF PAKISTAN" not in (by_num.get("18") or {}).get("section_text", "")
    assert "THE GAZETTE OF PAKISTAN" not in (by_num.get("29") or {}).get("section_text", "")
    title_11 = (by_num.get("11") or {}).get("section_title", "")
    assert "{l)" not in title_11
    assert title_11 == "Fund of the Authority"
    title_29 = (by_num.get("29") or {}).get("section_title", "")
    assert "{i)" not in title_29


def test_statute_staging_identity_url_rejects_listing_chrome_suffix():
    assert statute_staging_identity_url(PDF_URL, {"act_title": LISTING_CHROME_TITLE}) is None
    assert statute_staging_identity_url(PDF_URL, {"act_title": OFFICIAL_TITLE}) == f"{PDF_URL}|{OFFICIAL_TITLE[:200]}"


def test_live_na_pidama_pdf_has_sections_one_through_thirty_eight():
    httpx = pytest.importorskip("httpx")
    from scraper.parsers.pdf_extractor import extract_pdf_text

    pdf = httpx.get(PDF_URL, follow_redirects=True, timeout=60).content
    text = extract_pdf_text(pdf)
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={"source_name": "NationalAssembly", "url": PDF_URL},
    )
    assert extracted["statute_name"] == DETECTED_TITLE
    nums = sorted(int(s["section_number"]) for s in extracted["sections"] if str(s["section_number"]).isdigit())
    assert nums == list(range(1, 39))
    assert statute_staging_identity_url(PDF_URL, {"act_title": LISTING_CHROME_TITLE}) is None
    for section in extracted["sections"]:
        body = section.get("section_text", "")
        assert not contains_gazette_page_header_fragment(body)
