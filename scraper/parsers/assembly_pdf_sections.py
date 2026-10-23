"""Assembly-act PDF section filtering (TOC pages, bogus numbers, thin bodies)."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from scraper.parsers.statute_parser import SHORT_TITLE_CLAUSE_RE, _parse_numeric_section_number, _strip_section_prefix

ASSEMBLY_SOURCE_NAMES = frozenset(
    {
        "PunjabAssembly",
        "SindhAssembly",
        "KPAssembly",
        "BalochistanAssembly",
        "AJKAssembly",
        "GBAssembly",
        "NationalAssembly",
        "Senate",
    }
)

MIN_ASSEMBLY_BODY_CHARS = 60
TOC_HEADING_RE = re.compile(r"(?i)^\s*(?:table\s+of\s+contents|contents)\s*[:\-–—]?\s*$")
TOC_LINE_RE = re.compile(r"(?i)^\s*\d+[A-Z]?\s*[\.\)]\s+.+\s+\d{1,4}\s*$")
YEAR_ONLY_RE = re.compile(r"^\s*(19|20)\d{2}\s*\.?\s*$")
PAGE_NUMBER_BODY_RE = re.compile(r"^\s*\d{1,4}\s*\.?\s*$")


def is_assembly_source(source_name: Optional[str]) -> bool:
    return (source_name or "") in ASSEMBLY_SOURCE_NAMES


def is_table_of_contents_chunk(text: str) -> bool:
    chunk = (text or "").strip()
    if not chunk:
        return True
    # A statutory provision may discuss the "contents" of a register or document.
    # Only a standalone heading, not an occurrence inside substantive text, is TOC evidence.
    if any(TOC_HEADING_RE.fullmatch(line.strip()) for line in chunk.splitlines()[:6]):
        return True
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    if len(lines) >= 4:
        toc_like = sum(1 for ln in lines if TOC_LINE_RE.match(ln))
        if toc_like >= max(3, len(lines) // 2):
            return True
    return False


def is_heading_only_section(section_text: str) -> bool:
    body = _strip_section_prefix(section_text or "")
    if not body:
        return True
    if SHORT_TITLE_CLAUSE_RE.match(body):
        return True
    if len(body) < MIN_ASSEMBLY_BODY_CHARS and re.search(
        r"(?i)\b(short\s+title|extent|commencement|definitions)\b", body
    ):
        return True
    return False


def _is_bogus_section_number(num: str, body: str, prev_num: Optional[int]) -> bool:
    n = _parse_numeric_section_number(num)
    if n is None:
        return False
    if YEAR_ONLY_RE.match(num or "") or (1800 <= n <= 2100 and len((body or "").strip()) < 40):
        return True
    stripped = _strip_section_prefix(body)
    if PAGE_NUMBER_BODY_RE.match(stripped):
        return True
    if prev_num is not None and n < prev_num:
        return True
    if prev_num is not None and n > prev_num + 120:
        return True
    return False


def filter_assembly_pdf_sections(sections: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Drop TOC chunks and bogus section numbers from assembly PDF splits."""
    out: List[Dict[str, str]] = []
    prev: Optional[int] = None
    for s in sections:
        text = (s.get("section_text") or "").strip()
        if is_table_of_contents_chunk(text):
            continue
        num = str(s.get("section_number") or "").strip()
        if _is_bogus_section_number(num, text, prev):
            continue
        n = _parse_numeric_section_number(num)
        if n is not None:
            prev = n
        out.append(s)
    return out


def total_section_body_chars(sections: List[Dict[str, str]]) -> int:
    return sum(len(_strip_section_prefix(s.get("section_text") or "")) for s in sections)


def assembly_body_too_thin(sections: List[Dict[str, str]]) -> bool:
    if not sections:
        return True
    if all(is_heading_only_section(s.get("section_text") or "") for s in sections):
        return True
    return total_section_body_chars(sections) < MIN_ASSEMBLY_BODY_CHARS
