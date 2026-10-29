"""Reject English-OCR gibberish stored from Urdu-script statute PDFs.

The ops signal is a section of at least 80 characters whose text is more than
75% outside words of four or more Latin letters (``aeho a " x fo og``). That
is the shape of AJK Act II of 2023 on law.gok.pk. Urdu script and numeric
tariff schedules are a different text and stay.

A statute is refused when more than half of those long sections are noise.
One noisy section in an otherwise English act is a false section candidate,
not a reason to drop the act. Image-only Nastaliq still needs a dedicated
Urdu OCR pass; ``route_urdu_script_pdf`` in the PDF extractor is that hook.
"""

from __future__ import annotations

import re
from typing import Sequence

from scraper.statute_quality.ocr_quality import _is_numeric_schedule_section

REASON_GARBLED_OCR_UNREADABLE_SECTIONS = "garbled_ocr_unreadable_sections"

MIN_GARBLED_SECTION_CHARS = 80
# Strictly more than 75% of the characters sit outside 4+ letter words.
NON_LONG_WORD_FRACTION = 0.75
# More than half of the long sections. One bad section in a clean act stays.
LONG_SECTION_MAJORITY = 0.5
_LONG_WORD_RE = re.compile(r"[A-Za-z]{4,}")
# Arabic/Urdu blocks, plus InPage legacy letterforms in the private-use area.
_URDU_BLOCKS = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
    (0xE000, 0xF8FF),
)


def _is_urdu_script_char(ch: str) -> bool:
    code = ord(ch)
    return any(start <= code <= end for start, end in _URDU_BLOCKS)


def text_is_urdu_script(text: str, *, min_letters: int = 8) -> bool:
    """True when the letters are predominantly Urdu, Arabic, or InPage forms."""
    urdu = 0
    letters = 0
    for ch in text or "":
        if _is_urdu_script_char(ch):
            urdu += 1
            letters += 1
        elif ch.isalpha():
            letters += 1
    if letters < min_letters:
        return False
    return (urdu / letters) >= 0.5


def _non_long_word_fraction(text: str) -> float:
    total = len(text)
    if total == 0:
        return 0.0
    covered = sum(len(match.group(0)) for match in _LONG_WORD_RE.finditer(text))
    return (total - covered) / total


def text_has_readable_words(text: str) -> bool:
    """A text layer worth keeping ahead of an English OCR pass."""
    body = (text or "").strip()
    if text_is_urdu_script(body, min_letters=8):
        return True
    if len(body) < 40:
        return False
    covered = sum(len(match.group(0)) for match in _LONG_WORD_RE.finditer(body))
    if covered < 12:
        return False
    return _non_long_word_fraction(body) <= NON_LONG_WORD_FRACTION


def section_is_garbled_ocr(text: str) -> bool:
    """True for a long Latin section that is mostly not 4+ letter words."""
    body = (text or "").strip()
    if len(body) < MIN_GARBLED_SECTION_CHARS:
        return False
    if text_is_urdu_script(body, min_letters=8):
        return False
    if _is_numeric_schedule_section(body):
        return False
    return _non_long_word_fraction(body) > NON_LONG_WORD_FRACTION


def _section_bodies(sections: Sequence) -> list[str]:
    bodies: list[str] = []
    for section in sections or []:
        if isinstance(section, dict):
            bodies.append(str(section.get("section_text") or "").strip())
        elif isinstance(section, str):
            bodies.append(section.strip())
    return [body for body in bodies if body]


def garbled_ocr_unreadable_reason(*, name: str = "", sections: Sequence | None = None, raw_text: str = "") -> str:
    """Reason code when the statute body is English-OCR noise, else empty."""
    bodies = _section_bodies(sections or [])
    long = [body for body in bodies if len(body) >= MIN_GARBLED_SECTION_CHARS]
    garbled = [body for body in long if section_is_garbled_ocr(body)]
    if long and len(garbled) > len(long) * LONG_SECTION_MAJORITY:
        return REASON_GARBLED_OCR_UNREADABLE_SECTIONS
    if bodies:
        return ""
    raw = (raw_text or "").strip()
    if section_is_garbled_ocr(raw):
        return REASON_GARBLED_OCR_UNREADABLE_SECTIONS
    if len(raw) < MIN_GARBLED_SECTION_CHARS and section_is_garbled_ocr(name or ""):
        return REASON_GARBLED_OCR_UNREADABLE_SECTIONS
    return ""


def ocr_noise_should_yield_to_text_layer(text_layer: str, ocr_text: str) -> bool:
    """English OCR gibberish must not replace a text layer that already reads."""
    if not section_is_garbled_ocr(ocr_text):
        return False
    return text_has_readable_words(text_layer)
