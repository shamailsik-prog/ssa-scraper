"""English OCR of Urdu-script PDFs stores Latin noise as statute sections.

Ops heuristic: a section of at least 80 characters is unreadable when more than
75% of it is not a 4+ letter word. AJK Act II of 2023 on law.gok.pk is the
shape (every stored section is that noise). A clean English act must pass.
"""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path

from scraper.document_validation import validate_statute_reconciled_json
from scraper.parsers import pdf_extractor
from scraper.parsers.statute_parser import _is_false_section_candidate, split_into_sections
from scraper.statute_quality.garbled_ocr import (
    REASON_GARBLED_OCR_UNREADABLE_SECTIONS,
    garbled_ocr_unreadable_reason,
    section_is_garbled_ocr,
    text_is_urdu_script,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"

GARBLED = (FIXTURES / "ajk_act_ii_2023_garbled_section.txt").read_text(encoding="utf-8").strip()
CLEAN_LINES = [
    line.strip()
    for line in (FIXTURES / "clean_english_finance_act.txt").read_text(encoding="utf-8").splitlines()
    if line.strip()
]


def _sections(bodies: list[str]) -> list[dict[str, str]]:
    return [{"section_number": str(i), "section_text": body} for i, body in enumerate(bodies, start=1)]


def test_ajk_garbled_section_is_unreadable_noise():
    assert len(GARBLED) >= 80
    assert section_is_garbled_ocr(GARBLED)


def test_clean_english_section_is_not_garbled():
    for line in CLEAN_LINES:
        assert len(line) >= 80
        assert not section_is_garbled_ocr(line)


def test_short_noise_and_urdu_and_schedule_are_not_this_defect():
    assert not section_is_garbled_ocr("aeho a x fo")
    urdu = "دفعہ ۱۔ یہ ایکٹ آزاد جموں و کشمیر میں نافذ ہوگا اور حکومت قواعد بنا سکے گی۔ " * 3
    assert text_is_urdu_script(urdu)
    assert not section_is_garbled_ocr(urdu)
    inpage = "\ue000\ue001" * 50
    assert text_is_urdu_script(inpage)
    assert not section_is_garbled_ocr(inpage)
    schedule = (
        "SCHEDULE I — CUSTOMS DUTY\n"
        "Heading No | Description of goods | Rate of duty\n"
        + "\n".join(f"{i:04d}.{i:02d} | {i * 17:05d} | {i % 30}%" for i in range(1, 40))
    )
    assert len(schedule) >= 80
    assert not section_is_garbled_ocr(schedule)


def test_validate_rejects_statute_whose_sections_are_garbled_ocr():
    sections = _sections([GARBLED, GARBLED.replace("aeho", "oeha"), GARBLED])
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "The Azad Jammu and Kashmir Something Act, 2023",
            "sections": sections,
        },
        raw_text="\n".join(s["section_text"] for s in sections),
        source_name="AJKAssembly",
        source_url="https://law.gok.pk/wp-content/uploads/ajk-act-ii-2023.pdf",
    )
    assert ok is False
    assert reason == REASON_GARBLED_OCR_UNREADABLE_SECTIONS
    assert reason == "garbled_ocr_unreadable_sections"


def test_validate_passes_clean_english_control():
    sections = _sections(CLEAN_LINES)
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "The Azad Jammu and Kashmir Finance Act, 2023",
            "sections": sections,
        },
        raw_text="\n".join(CLEAN_LINES),
        source_name="AJKAssembly",
        source_url="https://law.gok.pk/wp-content/uploads/ajk-finance-act-2023.pdf",
    )
    assert ok is True
    assert reason == ""


def test_one_garbled_section_among_clean_ones_does_not_reject_the_statute():
    sections = _sections([CLEAN_LINES[0], GARBLED, CLEAN_LINES[1], CLEAN_LINES[2]])
    assert garbled_ocr_unreadable_reason(
        name="The Azad Jammu and Kashmir Finance Act, 2023",
        sections=sections,
        raw_text="",
    ) == ""
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "The Azad Jammu and Kashmir Finance Act, 2023", "sections": sections},
        raw_text="\n".join(CLEAN_LINES),
    )
    assert ok is True
    assert reason == ""


def test_majority_of_long_sections_garbled_rejects():
    sections = _sections([GARBLED, GARBLED, CLEAN_LINES[0]])
    assert garbled_ocr_unreadable_reason(name="Act", sections=sections, raw_text="") == (
        REASON_GARBLED_OCR_UNREADABLE_SECTIONS
    )


def test_false_section_candidate_drops_garbled_noise_and_keeps_english():
    assert _is_false_section_candidate(f"2. {GARBLED}")
    for line in CLEAN_LINES:
        assert not _is_false_section_candidate(line)
    urdu = "1. " + ("دفعہ ۱۔ یہ ایکٹ آزاد جموں و کشمیر میں نافذ ہوگا اور حکومت قواعد بنا سکے گی۔ " * 3)
    assert not _is_false_section_candidate(urdu)


def test_split_drops_garbled_candidate_and_keeps_english_sections():
    noise = "aeho a \" x fo og * toe ae he Bay ae ! 2h equals " * 6
    text = "\n".join([CLEAN_LINES[0], f"2. {noise}", CLEAN_LINES[2]])
    nums = [row["section_number"] for row in split_into_sections(text, "The Finance Act, 2023")]
    assert "1" in nums
    assert "3" in nums
    assert "2" not in nums


def test_unreadable_name_with_no_sections_uses_the_same_reason():
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": GARBLED, "sections": []},
        raw_text=GARBLED,
    )
    assert ok is False
    assert reason == "garbled_ocr_unreadable_sections"


def test_clean_body_without_sections_stays_empty_sections():
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "The Finance Act, 2023", "sections": []},
        raw_text="\n".join(CLEAN_LINES),
    )
    assert ok is False
    assert reason == "empty_statute_sections"


def test_urdu_text_layer_skips_english_ocr(monkeypatch):
    urdu = "دفعہ ۱۔ یہ ایکٹ آزاد جموں و کشمیر میں نافذ ہوگا اور حکومت قواعد بنا سکے گی۔ " * 4

    def _no_ocr(_pdf_bytes: bytes) -> str:
        raise AssertionError("English OCR must not replace an Urdu text layer")

    monkeypatch.setattr(pdf_extractor, "_pdfplumber_extract", lambda _b: (urdu, 2, True))
    monkeypatch.setattr(pdf_extractor, "_ocr_extract_with_pdfplumber_images", _no_ocr)
    text = pdf_extractor.extract_pdf_text(b"%PDF-1.4\n" + b"0" * 1200)
    assert "دفعہ" in text
    assert pdf_extractor.route_urdu_script_pdf(urdu) == urdu.strip()
    assert pdf_extractor.route_urdu_script_pdf(CLEAN_LINES[0]) is None


def test_garbled_english_ocr_does_not_replace_a_readable_text_layer(monkeypatch):
    primary = CLEAN_LINES[0]
    monkeypatch.setattr(pdf_extractor, "_pdfplumber_extract", lambda _b: (primary, 1, True))
    monkeypatch.setattr(pdf_extractor, "_ocr_extract_with_pdfplumber_images", lambda _b: GARBLED * 3)
    text = pdf_extractor.extract_pdf_text(b"%PDF-1.4\n" + b"0" * 1200)
    assert "Finance Act" in text
    assert "aeho" not in text


def test_image_only_garbled_ocr_is_kept_so_the_gate_can_name_it(monkeypatch):
    monkeypatch.setattr(pdf_extractor, "_pdfplumber_extract", lambda _b: ("", 1, True))
    monkeypatch.setattr(pdf_extractor, "_ocr_extract_with_pdfplumber_images", lambda _b: GARBLED)
    text = pdf_extractor.extract_pdf_text(b"%PDF-1.4\n" + b"0" * 1200)
    assert "aeho" in text


def test_short_urdu_ocr_does_not_fall_back_to_english(monkeypatch):
    urdu_page = "دفعہ یہ ایکٹ آزاد"  # under 50 chars, still Urdu letters
    assert len(urdu_page.strip()) < 50
    assert text_is_urdu_script(urdu_page, min_letters=8)
    calls: list[str] = []

    def image_to_string(_image, config="", timeout=0):
        calls.append(config)
        if "urd" in config:
            return urdu_page
        return GARBLED

    monkeypatch.setattr(pdf_extractor, "_ocr_limits", lambda: (0, 0))
    monkeypatch.setitem(sys.modules, "pytesseract", types.SimpleNamespace(image_to_string=image_to_string))
    # pdfplumber still has to open a real one-page PDF; tesseract itself is replaced.
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    drawing = canvas.Canvas(buf, pagesize=A4)
    img = Image.new("RGB", (400, 80), "white")
    ImageDraw.Draw(img).text((10, 30), "scan", fill="black")
    drawing.drawImage(ImageReader(img), 30, 700, width=400, height=80)
    drawing.showPage()
    drawing.save()
    text = pdf_extractor._ocr_extract_with_pdfplumber_images(buf.getvalue())
    assert "دفعہ" in text
    assert "aeho" not in text
    assert calls
    assert all("urd" in config for config in calls)


def test_validation_flag_off_still_accepts_garbled_payload(monkeypatch):
    monkeypatch.setattr("scraper.document_validation.settings.SSA_DOCUMENT_SAVE_VALIDATION", False)
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "Act", "sections": _sections([GARBLED, GARBLED])},
        raw_text=GARBLED,
    )
    assert ok is True
    assert reason == ""
