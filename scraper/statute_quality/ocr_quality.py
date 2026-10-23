"""OCR / extraction quality signals for visible statutes."""

from __future__ import annotations

import re
from typing import List, Tuple

REPLACEMENT_CHAR = "\ufffd"
MOJIBAKE_RUN_RE = re.compile(r"(?:Ã.|Â.|ï»¿|â€)")
GARBLED_NONWORD_RE = re.compile(r"[^\w\s\u0600-\u06FF.,;:'\"()\-–—/\\%&@#]+")
WORD_TOKEN_RE = re.compile(r"[A-Za-z\u0600-\u06FF]{3,}")

MIN_BODY_CHARS_FOR_OCR = 80
MAX_NONWORD_RATIO = 0.42
MAX_REPLACEMENT_DENSITY = 0.02
MIN_WORD_TOKEN_COUNT = 8


def _char_stats(text: str) -> Tuple[int, int, int, int]:
    total = len(text)
    if total == 0:
        return 0, 0, 0, 0
    replacements = text.count(REPLACEMENT_CHAR)
    nonword = len(GARBLED_NONWORD_RE.findall(text))
    words = len(WORD_TOKEN_RE.findall(text))
    return total, replacements, nonword, words


def ocr_quality_issues(text: str) -> List[str]:
    """Return issue codes; empty list means acceptable."""
    body = (text or "").strip()
    if len(body) < MIN_BODY_CHARS_FOR_OCR:
        return []
    total, replacements, nonword, words = _char_stats(body)
    issues: List[str] = []
    if replacements / total > MAX_REPLACEMENT_DENSITY:
        issues.append("replacement_char_density")
    if MOJIBAKE_RUN_RE.search(body):
        issues.append("mojibake")
    if words < MIN_WORD_TOKEN_COUNT and total > 200:
        issues.append("too_few_words")
    letter_like = sum(1 for ch in body if ch.isalpha())
    if letter_like / total < 0.35:
        issues.append("low_letter_ratio")
    if nonword / max(total, 1) > MAX_NONWORD_RATIO:
        issues.append("high_nonword_ratio")
    return issues


def statute_text_poor_ocr(*, name: str, section_texts: List[str]) -> List[str]:
    combined = "\n\n".join([name or "", *section_texts])
    return ocr_quality_issues(combined)
