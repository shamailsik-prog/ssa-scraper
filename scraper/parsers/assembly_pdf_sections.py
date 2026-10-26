"""Assembly-act PDF section filtering (TOC pages, bogus numbers, thin bodies)."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from scraper.parsers.statute_parser import (
    SHORT_TITLE_CLAUSE_RE,
    _derive_title,
    _extract_number_title_from_chunk,
    _parse_numeric_section_number,
    _strip_section_prefix,
)

# Gazette PDFs (na.gov.pk uploads) often embed page headers mid-section.
GAZETTE_PAGE_LINE_RE = re.compile(
    r"(?i)^\s*(?:\d{1,2},\d{4}\s*)?(?:\d{1,4}\s+)?THE\s+GAZETTE\s+OF\s+PAKISTAN\b.*$",
)
GAZETTE_PART_LINE_RE = re.compile(r"(?i)^\s*\[?\s*PART\s+I\s*\]?\s*$")

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
TOC_HEADING_RE = re.compile(r"(?i)\b(table\s+of\s+contents|contents)\b")
TOC_LINE_RE = re.compile(r"(?i)^\s*\d+[A-Z]?\s*[\.\)]\s+.+\s+\d{1,4}\s*$")
YEAR_ONLY_RE = re.compile(r"^\s*(19|20)\d{2}\s*\.?\s*$")
PAGE_NUMBER_BODY_RE = re.compile(r"^\s*\d{1,4}\s*\.?\s*$")


def is_assembly_source(source_name: Optional[str]) -> bool:
    return (source_name or "") in ASSEMBLY_SOURCE_NAMES


def _fix_roman_i_misread_as_section_eleven(text: str) -> str:
    """OCR often emits section 11 as a solitary 'I.' after section 10."""
    matches = list(re.finditer(r"(?m)^(\s*)I\.\s+", text))
    if not matches:
        return text
    out = text
    for m in reversed(matches):
        before = out[: m.start()]
        after = out[m.end() :]
        prev_nums = [int(x) for x in re.findall(r"(?m)^\s*(\d+)\.\s+", before)]
        next_nums = [int(x) for x in re.findall(r"(?m)^\s*(\d+)\.\s+", after[:4000])]
        if not prev_nums or prev_nums[-1] != 10:
            continue
        if not next_nums or next_nums[0] < 12:
            continue
        prefix = m.group(1)
        out = out[: m.start()] + f"{prefix}11. " + out[m.end() :]
    return out


def _normalize_gazette_ocr_braces(text: str) -> str:
    out = re.sub(r"(?m)\.-\{", ".-(", text)
    out = re.sub(r"\{(\d)", r"(\1", out)
    out = re.sub(r"\{l\)", "(1)", out, flags=re.IGNORECASE)
    return out


def normalize_assembly_gazette_pdf_text(text: str) -> str:
    """Fix common OCR section markers in federal gazette act PDFs before section splitting."""
    if not text:
        return text
    out = text
    out = re.sub(r"(?m)^(\s*)l\.\s+(?=Short\s+title\b)", r"\g<1>1. ", out, flags=re.IGNORECASE)
    out = re.sub(r"(?m)^(\s*)3l\.\s+", r"\g<1>31. ", out, flags=re.IGNORECASE)
    out = re.sub(r"(?m)^(\s*)ll\.\s+", r"\g<1>11. ", out, flags=re.IGNORECASE)
    out = _fix_roman_i_misread_as_section_eleven(out)
    out = _normalize_gazette_ocr_braces(out)
    return out


def strip_gazette_page_break_lines(body: str) -> str:
    """Remove gazette page chrome lines that pdfplumber leaves inside a section body."""
    kept: List[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if GAZETTE_PAGE_LINE_RE.match(stripped):
            continue
        if GAZETTE_PART_LINE_RE.match(stripped):
            continue
        if re.fullmatch(r"\d{1,2},\d{4}", stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def is_table_of_contents_chunk(text: str) -> bool:
    chunk = (text or "").strip()
    if not chunk:
        return True
    if TOC_HEADING_RE.search(chunk[:400]):
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
    if n == 0:
        return True
    if YEAR_ONLY_RE.match(num or "") or (1800 <= n <= 2100 and len((body or "").strip()) < 40):
        return True
    if 1800 <= n <= 2100:
        return True
    stripped = _strip_section_prefix(body)
    if PAGE_NUMBER_BODY_RE.match(stripped):
        return True
    if prev_num is not None and n < prev_num:
        return True
    if prev_num is not None and n > prev_num + 120:
        return True
    return False


def clean_assembly_section_records(sections: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Strip gazette page chrome and OCR brace glitches from stored section bodies."""
    for s in sections:
        text = _normalize_gazette_ocr_braces(s.get("section_text") or "")
        text = strip_gazette_page_break_lines(text)
        s["section_text"] = text
        num, title, _ = _extract_number_title_from_chunk(text)
        if num:
            derived = _derive_title(_strip_section_prefix(text))
            if derived:
                s["section_title"] = derived[:500]
    return sections


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
