"""OCR / extraction quality signals for visible statutes."""

from __future__ import annotations

import re
from typing import List, Tuple

REPLACEMENT_CHAR = "\ufffd"
MOJIBAKE_RUN_RE = re.compile(r"(?:Ã.|Â.|ï»¿|â€)")
WORD_TOKEN_RE = re.compile(r"[A-Za-z\u0600-\u06FF]{3,}")
NUMERIC_SCHEDULE_RE = re.compile(
    r"(?i)\b("
    r"schedule|tariff|customs\s+duty|serial\s+no|description\s+of\s+goods|"
    r"heading\s+no|rate\s+of\s+duty|hs\s+code"
    r")\b"
)

MIN_SECTION_CHARS = 40
MOJIBAKE_DENSITY_THRESHOLD = 0.012
MIN_LETTER_RATIO = 0.28
MIN_LETTER_RATIO_SCHEDULE = 0.12
MAJORITY_FAIL_FRACTION = 0.5


def _mojibake_density(text: str) -> float:
    if not text:
        return 0.0
    hits = len(MOJIBAKE_RUN_RE.findall(text))
    return hits / max(len(text), 1)


def _letter_ratio(text: str) -> float:
    total = len(text or "")
    if total == 0:
        return 0.0
    letters = sum(1 for ch in text if ch.isalpha())
    return letters / total


def _is_numeric_schedule_section(text: str) -> bool:
    body = (text or "").strip()
    if not NUMERIC_SCHEDULE_RE.search(body):
        return False
    digits = sum(1 for ch in body if ch.isdigit())
    return digits / max(len(body), 1) > 0.08


def section_fails_ocr_quality(section_text: str) -> bool:
    body = (section_text or "").strip()
    if len(body) < MIN_SECTION_CHARS:
        return False
    if _mojibake_density(body) >= MOJIBAKE_DENSITY_THRESHOLD:
        return True
    schedule = _is_numeric_schedule_section(body)
    min_ratio = MIN_LETTER_RATIO_SCHEDULE if schedule else MIN_LETTER_RATIO
    if _letter_ratio(body) < min_ratio:
        return True
    if body.count(REPLACEMENT_CHAR) >= 3:
        return True
    return False


def ocr_quality_issues(text: str) -> List[str]:
    """Single-chunk diagnostic (tests); prefer statute_text_poor_ocr for corpus passes."""
    if section_fails_ocr_quality(text):
        return ["section_poor_ocr"]
    return []


def statute_text_poor_ocr(*, name: str, section_texts: List[str]) -> List[str]:
    bodies = [t for t in section_texts if (t or "").strip()]
    if not bodies:
        if section_fails_ocr_quality(name or ""):
            return ["majority_sections_poor_ocr"]
        return []
    fails = sum(1 for t in bodies if section_fails_ocr_quality(t))
    if fails > len(bodies) * MAJORITY_FAIL_FRACTION:
        return ["majority_sections_poor_ocr"]
    return []
