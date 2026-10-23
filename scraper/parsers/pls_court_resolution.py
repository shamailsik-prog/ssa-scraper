"""Resolve PakistanLawSite court from preserved text when the grid row defaults to SC Pakistan."""

from __future__ import annotations

import re
from typing import Optional

from scraper.parsers.case_metadata import judgment_header_slice, parse_pls_header_court
from scraper.parsers.citation_extractor import court_from_bracket, canonical_court
from scraper.parsers.judgment_header_court import court_from_judgment_header

_FOREIGN_COUNTRY = re.compile(
    r"(?i)\b(?:india|bangladesh|england|u\.?k\.?|united\s+kingdom|sri\s+lanka)\b",
)
_HEADER_FOREIGN_BRACKET = re.compile(
    r"(?im)^\s*\[\s*((?:Supreme|High)\s+Court[^\]]*)\s*\]\s*$",
)
_HEADER_FOREIGN_INLINE = re.compile(
    r"(?im)\[\s*((?:Supreme|High)\s+Court[^\]]*(?:"
    r"india|bangladesh|england|u\.?k|sri\s+lanka)[^\]]*)\s*\]",
)
_SC_PAKISTAN = re.compile(r"(?i)^supreme\s+court\s+of\s+pakistan$")
_PLS_COURT_LINE = re.compile(
    r"(?im)\b(?:IN\s+THE\s+)?(SUPREME\s+COURT\s+OF\s+PAKISTAN|"
    r"LAHORE\s+HIGH\s+COURT|SINDH\s+HIGH\s+COURT|PESHAWAR\s+HIGH\s+COURT|"
    r"BALOCHISTAN\s+HIGH\s+COURT|ISLAMABAD\s+HIGH\s+COURT|"
    r"FEDERAL\s+SHARIAT\s+COURT|FEDERAL\s+CONSTITUTIONAL\s+COURT)\b",
)
def is_default_supreme_court_of_pakistan(court: Optional[str]) -> bool:
    return bool(court and _SC_PAKISTAN.match(re.sub(r"\s+", " ", court.strip())))


def _title_case_court(label: str) -> str:
    cleaned = re.sub(r"\s+", " ", label.strip(" .,-"))
    if not cleaned:
        return cleaned
    if _FOREIGN_COUNTRY.search(cleaned):
        return cleaned[:255]
    known = canonical_court(cleaned)
    return known or cleaned[:255]


def court_from_pls_header_block(text: Optional[str]) -> Optional[str]:
    """Court from the judgment header (bracket line, IN THE line, or foreign bracket in first ~300 chars)."""
    if not text:
        return None
    head300 = (text or "")[:300]
    m = _HEADER_FOREIGN_INLINE.search(head300)
    if m and _FOREIGN_COUNTRY.search(m.group(1)):
        return _title_case_court(m.group(1))
    header = judgment_header_slice(text)
    for line in header.splitlines()[:8]:
        bm = _HEADER_FOREIGN_BRACKET.match(line)
        if bm and _FOREIGN_COUNTRY.search(bm.group(1)):
            return _title_case_court(bm.group(1))
    pak_line = _PLS_COURT_LINE.search(header)
    if pak_line:
        name = re.sub(r"\s+", " ", pak_line.group(1)).strip()
        if name.upper() == "SUPREME COURT OF PAKISTAN":
            return "Supreme Court of Pakistan"
        return name.title()
    in_the = court_from_judgment_header(text)
    if in_the and _FOREIGN_COUNTRY.search(in_the):
        return _title_case_court(in_the)
    if in_the and re.search(r"(?i)\bhigh\s+court\b", in_the):
        return _title_case_court(in_the)
    parsed = parse_pls_header_court(text)
    if parsed and _FOREIGN_COUNTRY.search(parsed):
        return _title_case_court(parsed)
    if in_the:
        return _title_case_court(in_the)
    if parsed:
        return _title_case_court(parsed)
    return None


def court_from_pls_bracket_after_citation(text: str, citation_end: Optional[int]) -> Optional[str]:
    if citation_end is None:
        return None
    bracket = court_from_bracket(text, citation_end)
    if bracket and _FOREIGN_COUNTRY.search(bracket):
        return _title_case_court(bracket)
    return None


def resolve_pls_court(
    row_court: Optional[str],
    text: Optional[str],
    *,
    citation_end: Optional[int] = None,
) -> Optional[str]:
    """Prefer header/bracket foreign court over a grid default of Supreme Court of Pakistan."""
    header_court = court_from_pls_header_block(text)
    bracket_court = court_from_pls_bracket_after_citation(text or "", citation_end) if text else None
    text_court = bracket_court or header_court
    if text_court and is_default_supreme_court_of_pakistan(row_court):
        if _FOREIGN_COUNTRY.search(text_court) or (
            header_court and not is_default_supreme_court_of_pakistan(header_court)
        ):
            return text_court
    if text_court and row_court and not is_default_supreme_court_of_pakistan(row_court):
        return row_court
    if row_court:
        return row_court
    return text_court


def sc_hc_mislabel_fix(stored_court: Optional[str], text: Optional[str]) -> Optional[str]:
    """When stored court is SC Pakistan but the header names a High Court (or vice versa), return header court."""
    if not text or not stored_court:
        return None
    header = court_from_judgment_header(text) or court_from_pls_header_block(text)
    if not header:
        return None
    stored_low = stored_court.lower()
    header_low = header.lower()
    if "supreme court" in stored_low and "high court" in header_low and "pakistan" in stored_low:
        return _title_case_court(header)
    if "high court" in stored_low and "supreme court" in header_low and "pakistan" in header_low:
        return _title_case_court(header)
    return None


def proposed_court_name(stored_court: Optional[str], text: Optional[str], *, citation_end: Optional[int] = None) -> Optional[str]:
    """Single proposed court_name for backfill (foreign mislabel + SC/HC swap)."""
    swap = sc_hc_mislabel_fix(stored_court, text)
    if swap and swap != stored_court:
        return swap
    resolved = resolve_pls_court(stored_court, text, citation_end=citation_end)
    if resolved and resolved != stored_court:
        return resolved
    if is_default_supreme_court_of_pakistan(stored_court):
        header = court_from_pls_header_block(text)
        if header and header != stored_court and not is_default_supreme_court_of_pakistan(header):
            return header
    return None


def foreign_court_mislabel_sql_predicate() -> str:
    """SQL fragment: heading names a foreign SC/HC but court_name is SC Pakistan."""
    return r"""left(j.full_text, 300) ~* '\[(Supreme Court|High Court)[^\]]*(India|Bangladesh|England|U\.?K|Sri Lanka)'
      AND j.court_name ILIKE 'Supreme Court of Pakistan'"""
