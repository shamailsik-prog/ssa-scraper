"""Resolve PakistanLawSite court from preserved text when the grid row defaults to SC Pakistan."""

from __future__ import annotations

import re
from typing import Optional

from scraper.parsers.case_metadata import judgment_header_slice
from scraper.parsers.citation_extractor import PLD_COURT_CANONICAL, canonical_court, court_from_bracket, normalise_citation

_PAK_HC_CANONICAL = frozenset(
    v
    for k, v in PLD_COURT_CANONICAL.items()
    if "HIGH COURT" in k or v in ("Lahore", "Sindh", "Peshawar", "Balochistan", "Islamabad", "Karachi")
)

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
_HEADER_TOP_HC_BRACKET = re.compile(
    r"(?im)^\s*\[\s*((?:high\s+court|lahore|peshawar|sindh|balochistan|islamabad)[^\]]*)\s*\]\s*$",
)
_HEADER_TOP_IN_THE_HC = re.compile(
    r"(?im)^\s*IN\s+THE\s+(HIGH\s+COURT[^\n,]{0,100})",
)
_HEADER_TOP_IN_THE_SC_PAK = re.compile(
    r"(?im)^\s*IN\s+THE\s+SUPREME\s+COURT\s+OF\s+PAKISTAN\b",
)
_PLD_SC_SCMR = re.compile(
    r"(?i)^(?:PLD\s+\d{4}\s+SC\b|SCMR\s+\d{4}|\d{4}\s+SCMR\b)",
)


def is_default_supreme_court_of_pakistan(court: Optional[str]) -> bool:
    return bool(court and _SC_PAKISTAN.match(re.sub(r"\s+", " ", court.strip())))


def citation_is_pld_sc_or_scmr(citation: Optional[str]) -> bool:
    norm = normalise_citation(citation or "") or (citation or "").strip()
    return bool(norm and _PLD_SC_SCMR.match(norm))


def _header_top_block(text: str, *, max_lines: int = 3) -> str:
    header = judgment_header_slice(text)
    return "\n".join(header.splitlines()[:max_lines])


_GEOGRAPHIC_PAREN = re.compile(
    r"\((?:england|bangladesh|sri\s+lanka|india|u\.?k\.?|united\s+kingdom)\)",
    re.I,
)


def _normalize_foreign_display(label: str) -> str:
    cleaned = re.sub(r"\s+", " ", label.strip(" .,-"))
    low = cleaned.lower()
    if "supreme" in low and "india" in low:
        return "Supreme Court of India"
    if "supreme" in low and re.search(r"(?:u\.?k|england|united\s+kingdom)", low):
        return "Supreme Court of UK"
    if "supreme" in low and re.search(r"azad|jammu|kashmir", low):
        return "Azad Jammu and Kashmir Supreme Court"
    if re.search(r"(?i)\bhigh\s+court\b", cleaned) and _GEOGRAPHIC_PAREN.search(cleaned):
        return cleaned[:255]
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()[:255]


def _accepted_court_name(label: str) -> Optional[str]:
    if not label or not label.strip():
        return None
    low = label.lower()
    if "supreme" in low and re.search(r"aj&?k|azad|jammu|kashmir", low):
        return "Azad Jammu and Kashmir Supreme Court"
    normalized = _normalize_foreign_display(label)
    if _FOREIGN_COUNTRY.search(normalized):
        return normalized
    known = canonical_court(normalized)
    if known:
        return known
    if re.search(r"(?i)\bhigh\s+court\b", normalized):
        known_hc = canonical_court(normalized)
        return known_hc
    if _SC_PAKISTAN.match(normalized):
        return "Supreme Court of Pakistan"
    return None


def foreign_court_from_first_300_chars(text: Optional[str]) -> Optional[str]:
    """Foreign Supreme/High Court named in the first 300 characters only."""
    if not text:
        return None
    head300 = text[:300]
    m = _HEADER_FOREIGN_INLINE.search(head300)
    if m and _FOREIGN_COUNTRY.search(m.group(1)):
        return _accepted_court_name(m.group(1))
    for line in head300.splitlines()[:8]:
        bm = _HEADER_FOREIGN_BRACKET.match(line)
        if bm and (
            _FOREIGN_COUNTRY.search(bm.group(1))
            or re.search(r"(?i)aj&?k|azad|jammu|kashmir", bm.group(1))
        ):
            return _accepted_court_name(bm.group(1))
    return None


def court_from_pls_header_block(text: Optional[str]) -> Optional[str]:
    """Foreign heading court only (no Pakistani court-line or body-text fallbacks)."""
    return foreign_court_from_first_300_chars(text)


def court_from_pls_bracket_after_citation(text: str, citation_end: Optional[int]) -> Optional[str]:
    if citation_end is None:
        return None
    bracket = court_from_bracket(text, citation_end)
    if bracket and _FOREIGN_COUNTRY.search(bracket):
        return _accepted_court_name(bracket)
    return None


def resolve_pls_court(
    row_court: Optional[str],
    text: Optional[str],
    *,
    citation_end: Optional[int] = None,
) -> Optional[str]:
    """Override grid SC Pakistan only when a foreign court is named in the heading window."""
    if not is_default_supreme_court_of_pakistan(row_court):
        return row_court
    foreign = foreign_court_from_first_300_chars(text)
    if foreign:
        return foreign
    bracket = court_from_pls_bracket_after_citation(text or "", citation_end) if text else None
    if bracket:
        return bracket
    return row_court


def _accepted_high_court_label(label: str) -> Optional[str]:
    raw = re.sub(r"\s+", " ", label.strip())
    if re.search(r"(?i)\bhigh\s+court\b", raw) and re.search(r"(?i)\bsindh\b", raw):
        return "Sindh"
    accepted = _accepted_court_name(label)
    if not accepted:
        return None
    if re.search(r"(?i)\bhigh\s+court\b", accepted):
        return accepted
    canon = canonical_court(accepted) or accepted
    if canon in _PAK_HC_CANONICAL:
        return canon
    return None


def _header_sc_hc_swap_court(text: str) -> Optional[str]:
    """SC↔HC swap signal from the first three header lines only (not appeal-from or body text)."""
    block = _header_top_block(text, max_lines=3)
    for line in block.splitlines():
        bm = _HEADER_TOP_HC_BRACKET.match(line)
        if bm:
            hc = _accepted_high_court_label(bm.group(1))
            if hc:
                return hc
    m = _HEADER_TOP_IN_THE_HC.search(block)
    if m:
        hc = _accepted_high_court_label(m.group(1))
        if hc:
            return hc
    if _HEADER_TOP_IN_THE_SC_PAK.search(block):
        return "Supreme Court of Pakistan"
    return None


def sc_hc_mislabel_fix(
    stored_court: Optional[str],
    text: Optional[str],
    *,
    citation: Optional[str] = None,
) -> Optional[str]:
    if not text or not stored_court or citation_is_pld_sc_or_scmr(citation):
        return None
    header_court = _header_sc_hc_swap_court(text)
    if not header_court:
        return None
    stored_low = stored_court.lower()
    header_low = header_court.lower()
    if "supreme court" in stored_low and "pakistan" in stored_low:
        return header_court
    if "high court" in stored_low and header_court == "Supreme Court of Pakistan":
        return header_court
    return None


def proposed_court_name(
    stored_court: Optional[str],
    text: Optional[str],
    *,
    citation: Optional[str] = None,
    citation_end: Optional[int] = None,
) -> Optional[str]:
    swap = sc_hc_mislabel_fix(stored_court, text, citation=citation)
    if swap and swap != stored_court:
        return swap
    if is_default_supreme_court_of_pakistan(stored_court):
        resolved = resolve_pls_court(stored_court, text, citation_end=citation_end)
        if resolved and resolved != stored_court:
            return resolved
    return None


def foreign_court_mislabel_sql_predicate() -> str:
    """SQL fragment: heading names a foreign SC/HC but court_name is SC Pakistan."""
    return r"""(
        left(j.full_text, 300) ~* '\[(Supreme Court|High Court)[^\]]*(India|Bangladesh|England|U\.?K|Sri Lanka)'
        OR left(j.full_text, 300) ~* '\[Supreme Court \(AJ&K\)\]'
        OR left(j.full_text, 300) ~* '\[Supreme Court \(AJ&amp;K\)\]'
        OR left(j.full_text, 300) ~* 'Azad Jammu[^\n]{0,40}Supreme Court'
      )
      AND j.court_name ILIKE 'Supreme Court of Pakistan'"""
