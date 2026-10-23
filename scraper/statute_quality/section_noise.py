"""Reject bogus section numbers and printer/gazette noise in statute section text."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from scraper.parsers.statute_parser import (
    GAZETTE_FOOTNOTE_RE,
    _is_false_section_candidate,
    _parse_numeric_section_number,
    _strip_section_prefix,
)

VALID_SECTION_SUFFIX_RE = re.compile(r"^\d+[A-Z]{1,2}$", re.IGNORECASE)
OCR_NAME_SPLIT_NUM_RE = re.compile(r"^\d+N$", re.IGNORECASE)
PAGE_ONLY_BODY_RE = re.compile(r"^\s*\d{1,4}\s*\.?\s*$")
PRINTERS_IMPRINT_RE = re.compile(
    r"(?i)\b("
    r"printed\s+at|printed\s+by|published\s+by|manager\s+printing|"
    r"printing\s+corporation|government\s+press|controller\s+of\s+printing"
    r")\b"
)
GAZETTE_HEADER_NOISE_RE = re.compile(
    r"(?i)\b("
    r"gazette\s+of\s+pakistan|extraordinary|part\s+[ivxlc]+|"
    r"islamabad,?\s+|rawalpindi,?\s+|muzaffarabad,?\s+"
    r")\b"
)
PUBLICATION_NOTICE_BODY_RE = re.compile(
    r"(?i)^\s*(?:section\s+\d+[A-Z]?\s*[\.\-\)\:]?\s*)?"
    r"(?:it\s+is\s+hereby\s+notified|published\s+in\s+the\s+gazette|assented\s+to\s+by|"
    r"received\s+the\s+assent|sanctioned\s+by)\b"
)


def is_bogus_section_number_token(section_number: str, section_text: str = "") -> bool:
    num = (section_number or "").strip()
    if not num:
        return True
    if OCR_NAME_SPLIT_NUM_RE.fullmatch(num):
        body = _strip_section_prefix(section_text or "")
        if re.search(r"(?i)\b(name|short\s+title)\b", body):
            return True
        return False
    if VALID_SECTION_SUFFIX_RE.fullmatch(num):
        return False
    n = _parse_numeric_section_number(num)
    if n is not None and n > 5000:
        return True
    return False


def is_section_body_noise(section_text: str) -> bool:
    body = _strip_section_prefix(section_text or "")
    if not body:
        return True
    if _is_false_section_candidate(section_text or ""):
        return True
    if PAGE_ONLY_BODY_RE.match(body):
        return True
    if PUBLICATION_NOTICE_BODY_RE.match(body) and len(body) < 320:
        return True
    if PRINTERS_IMPRINT_RE.search(body) and len(body) < 220:
        return True
    if GAZETTE_HEADER_NOISE_RE.search(body) and len(body) < 280 and not re.search(
        r"(?i)\b(shall|means|subject to|notwithstanding)\b", body
    ):
        return True
    return False


def filter_statute_quality_sections(
    sections: List[Dict[str, str]],
    *,
    statute_name: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Drop sections that are page numbers, OCR split artifacts, or imprint/header noise."""
    out: List[Dict[str, str]] = []
    for s in sections:
        num = str(s.get("section_number") or "").strip()
        text = (s.get("section_text") or "").strip()
        if is_bogus_section_number_token(num, text):
            continue
        if is_section_body_noise(text):
            continue
        out.append(s)
    return out
