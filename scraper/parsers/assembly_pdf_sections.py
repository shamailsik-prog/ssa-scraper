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
_GAZETTE_NAME_RE = re.compile(
    r"(?i)\bTHE\s+(?:G[A-Z]{0,2}ZET{1,2}E|CAZETTE|GAZETIE)\s+OF\s+PAKISTAN\b",
)
_GAZETTE_HEADER_DATE_RE = re.compile(
    r"(?i)(?:"
    r"\bOCT\.?\s*\d{1,2}\s*,?\s*\d{4}"
    r"|EXTRA\.?\s*,?\s*OCT\.?"
    r"|OCT\.?\s*\[?\s*PART\s*I"
    r"|\d{1,2},\d{4}\s*(?:\[?\s*PART\s*I)?\s*$"
    r")"
)
_GAZETTE_EXTRA_PART_HEADER_RE = re.compile(
    r"(?i)\b(?:G[A-Z]{0,2}ZET{1,2}E|CAZETTE)\s+OF\s+PAKISTAN\b"
    r"[^.\n]{0,120}?\bEXTRA\.?"
    r"[^.\n]{0,60}?\bPAR[T]?[R]?\s*I\b",
)
_HEADER_TOKEN_STRIP_RE = re.compile(
    r"(?i)(?:"
    r"THE\s+(?:G[A-Z]{0,2}ZET{1,2}E|CAZETTE)\s+OF\s+PAKISTAN"
    r"|Gazette\s+of\s+Pakistan"
    r"|EXTRA(?:ORDINARY)?"
    r"|OCT\.?"
    r"|PAR[T]?[R]?\s*I"
    r"|PUBLISHED\s+BY\s+AUTHORITY"
    r"|\d{1,2},\d{4}"
    r"|^\d{1,4}\s+"
    r")"
)
_OCR_GAZETTE_PAGE_AT_START_RE = re.compile(
    r"(?i)^\s*(?:\d{1,4}\s+THE\s+(?:G[A-Z]{0,2}ZET{1,2}E|CAZETTE)|\d[a-z0-9]\d\s+)"
)
_OCR_GAZETTE_PAGE_AT_END_RE = re.compile(r"(?i)\b\d[a-z0-9]\d\s*$")
_PART_STUB_LINE_RE = re.compile(r"(?i)^\[PART\s*$")

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
    out = re.sub(r"\{i\)", "(i)", out, flags=re.IGNORECASE)
    return out


def contains_gazette_page_header_fragment(text: str) -> bool:
    """True when text still carries pdfplumber page-header chrome (not substantive Gazette citations)."""
    if not text:
        return False
    for line in text.splitlines():
        if _is_gazette_page_header_line(line.strip()):
            return True
    return False


def _lowercase_word_count_after_header_tokens(stripped: str) -> int:
    scratch = _HEADER_TOKEN_STRIP_RE.sub(" ", stripped)
    scratch = re.sub(r"[^A-Za-z\s]", " ", scratch)
    count = 0
    for word in scratch.split():
        if len(word) < 2:
            continue
        if word.isupper():
            continue
        if any(ch.islower() for ch in word):
            count += 1
    return count


def _is_substantive_gazette_citation_line(stripped: str) -> bool:
    """Keep Gazette mentions that read as operative prose, not page-header chrome."""
    if not re.search(r"(?i)\bGazette\s+of\s+Pakistan\b", stripped) and not _GAZETTE_NAME_RE.search(stripped):
        return False
    return _lowercase_word_count_after_header_tokens(stripped) >= 3


def _has_gazette_header_date(stripped: str) -> bool:
    return bool(_GAZETTE_HEADER_DATE_RE.search(stripped))


def _has_ocr_gazette_page_number_at_edge(stripped: str) -> bool:
    return bool(_OCR_GAZETTE_PAGE_AT_START_RE.match(stripped) or _OCR_GAZETTE_PAGE_AT_END_RE.search(stripped))


def _has_gazette_extra_part_header(stripped: str) -> bool:
    return bool(_GAZETTE_EXTRA_PART_HEADER_RE.search(stripped))


# A running header the scan damaged (``TFIE GAZET^|E``, ``GAZBTTE OF PAXIS'IAN``).
# A real citation has several lowercase words or an operative verb, and is kept.
_OCR_RUNNING_HEADER_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_OCR_RUNNING_HEADER_PROSE_RE = re.compile(
    r"(?i)\b(?:shall|means|hereby|provided|notification|official|offtcial)\b"
)
_PRINTER_IMPRINT_PROSE_RE = re.compile(
    r"(?i)\b(?:shall|means|hereby|provided|applicant|partner|director)\b"
)
# Page number plus year (``298 1976``, ``296 1976 l-'``), not a sentence ending ``1976.``.
_PAGE_AND_YEAR_RE = re.compile(r"^\d{1,4}\s+(?:19|20)\d{2}\b")
_HEADER_STUB_RE = re.compile(
    r"^(?:[A-Z]{2,4}|\([A-Za-z]{3,8}|[A-Za-z]{1,6}['’\-][A-Za-z0-9]{1,4},?)$"
)
_HEADER_STUB_WORDS = frozenset({"AND", "THE", "FOR", "NOT", "ANY", "ACT", "BUT", "NOR", "OR"})


def _is_ocr_gazette_running_header(stripped: str) -> bool:
    """True for a gazette running header whose letters the scan broke.

    ``THE GAZETTE OF PAKISTAN, EXTRA.`` is already a header. This also matches
    ``TFIE GAZET^|E oF PAKISTAN, EXTRA..`` and ``GAZBTTE OF PAXIS'IAN``.
    A line with three lowercase words, or with ``shall`` / ``official Gazette``,
    stays. That is a citation, not furniture.
    """
    if not stripped or len(stripped) > 180:
        return False
    if _OCR_RUNNING_HEADER_PROSE_RE.search(stripped):
        return False
    words = _OCR_RUNNING_HEADER_WORD_RE.findall(stripped)
    lower = [word for word in words if any(char.islower() for char in word)]
    if len(lower) >= 3:
        return False
    folded = re.sub(r"[^A-Za-z]", "", stripped).upper()
    if not re.search(r"GAZ[A-Z]{0,6}T", folded):
        return False
    if not re.search(r"PAK|PAX", folded):
        return False
    if not re.search(
        r"EXTRA|XTRA|PART|PANR|HTRA|MAY|JUNE|JULY|JAN|FEB|MAR|APR|AUG|SEP|OCT|NOV|DEC",
        folded,
    ):
        return False
    return True


def _is_printer_imprint_line(stripped: str) -> bool:
    """True for the printer's closing line, not a sentence that mentions a manager."""
    if not stripped or len(stripped) > 220:
        return False
    if _PRINTER_IMPRINT_PROSE_RE.search(stripped):
        return False
    if re.search(r"(?i)\bprinting\s+corporation\b", stripped):
        return True
    if re.search(r"(?i)\bmanager\s+of\s+publications\b", stripped):
        return True
    if re.search(r"(?i)\bmanaqer\b", stripped) and re.search(r"(?i)\b(?:press|print|corporation)\b", stripped):
        return True
    if re.search(r"(?i)\bprinted\s+by\b", stripped) and re.search(
        r"(?i)\b(?:manager|press|corporation)\b", stripped
    ):
        return True
    if re.search(r"(?i)\bpublished\s+by\b", stripped) and re.search(
        r"(?i)\b(?:manager|publications|press)\b", stripped
    ):
        return True
    return False


def _is_page_furniture_stub(stripped: str) -> bool:
    """A slug or page-and-year line that sits directly above a running header.

    ``1976.`` is the end of a sentence and is not furniture. ``1976`` with no
    period, directly above the header, is the page's year.
    """
    if not stripped or len(stripped) > 24:
        return False
    if stripped in _HEADER_STUB_WORDS:
        return False
    if re.fullmatch(r"(?:18|19|20)\d{2}\.", stripped):
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", stripped):
        return True
    if _PAGE_AND_YEAR_RE.match(stripped):
        return True
    return bool(_HEADER_STUB_RE.match(stripped))


def _is_gazette_page_header_line(stripped: str) -> bool:
    if not stripped:
        return False
    if re.fullmatch(r"\d{1,2},\d{4}", stripped):
        return True
    if _PART_STUB_LINE_RE.match(stripped):
        return True
    if _is_substantive_gazette_citation_line(stripped):
        return False
    if not _GAZETTE_NAME_RE.search(stripped):
        return False
    if len(stripped) > 280:
        return False
    return (
        _has_gazette_header_date(stripped)
        or _has_ocr_gazette_page_number_at_edge(stripped)
        or _has_gazette_extra_part_header(stripped)
    )


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
    """Remove gazette page chrome lines that pdfplumber leaves inside a section body.

    A running header also drops the page-number line and the short scan slug
    directly above it (``(estr`` / ``298 1976`` / the header). A year that ends
    with a period stays, because that is the previous sentence. Printer imprint
    lines are dropped on their own.
    """
    lines = (body or "").splitlines()
    drop = [False] * len(lines)
    header_at: List[int] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        header = _is_gazette_page_header_line(stripped) or _is_ocr_gazette_running_header(stripped)
        if header or _is_printer_imprint_line(stripped):
            drop[index] = True
            if header:
                header_at.append(index)
    for index in header_at:
        cursor = index - 1
        while cursor >= 0:
            stripped = lines[cursor].strip()
            if not stripped or drop[cursor]:
                cursor -= 1
                continue
            if not _is_page_furniture_stub(stripped):
                break
            drop[cursor] = True
            cursor -= 1
    kept = [line for index, line in enumerate(lines) if not drop[index]]
    return _collapse_section_body_whitespace("\n".join(kept).strip())


def _collapse_section_body_whitespace(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())


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


def _derive_assembly_section_title(section_number: str, section_text: str) -> str:
    body = _strip_section_prefix(section_text or "")
    if section_number == "11" and re.search(r"(?i)\bAuthority\s+Fund\b", body):
        return "Fund of the Authority"
    derived = _derive_title(body)
    return derived[:500] if derived else ""


def clean_assembly_section_records(sections: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Strip gazette page chrome and OCR brace glitches from stored section bodies."""
    for s in sections:
        text = _normalize_gazette_ocr_braces(s.get("section_text") or "")
        text = strip_gazette_page_break_lines(text)
        s["section_text"] = text
        num = str(s.get("section_number") or "").strip()
        if num:
            title = _derive_assembly_section_title(num, text)
            if title:
                s["section_title"] = title
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
