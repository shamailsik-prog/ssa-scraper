"""Search-harvest known-row matching: court spelling variants the site prints differently from stored keys.

PLD high-court Aj&K is the main gap (site prints AJ&K / AJK / full name; corpus keys use Aj&K). PLD SC (AJ&K)
and year-first (Aj&K) paren series share the same alias set. Other PLD courts (Lahore, GB, IHC, …) already
collapse through normalise_citation; Gilgit uses GB without a mixed-case alias."""

from __future__ import annotations

import re
from typing import Set

from scraper.parsers.citation_extractor import normalise_citation

_AJK_TOKEN_RE = re.compile(
    r"^(?:AJ&K|AJK|AJ\s*&\s*K|A\.?\s*J\.?\s*&\s*K\.?|AZAD\s+JAMMU\s+AND\s+KASHMIR)$",
    re.IGNORECASE,
)
_PLD_AJK_HC_RE = re.compile(
    r"^PLD\s+(?P<year>\d{4})\s+(?P<court>.+?)\s+(?P<page>\d+[A-Z]?)$",
    re.IGNORECASE,
)
_PLD_AJK_SC_RE = re.compile(
    r"^PLD\s+(?P<year>\d{4})\s+SC\s*\(\s*(?P<court>[^)]+)\s*\)\s+(?P<page>\d+[A-Z]?)$",
    re.IGNORECASE,
)
_YEAR_FIRST_PAREN_RE = re.compile(
    r"^(?P<year>\d{4})\s+(?P<reporter>[A-Za-z]+(?:\s+[A-Za-z]+)?(?:\s+Note)?)\s*\(\s*(?P<court>[^)]+)\s*\)\s+(?P<page>\d+[A-Z]?)$",
    re.IGNORECASE,
)

# Stored PLD high-court keys use Aj&K; PakistanLawSite often prints AJ&K or AJK.
PLD_AJK_HC_LABELS = ("Aj&K", "AJ&K", "AJK", "A.J.&K.")
PLD_AJK_SC_LABELS = ("SC (AJ&K)", "SC (Aj&K)", "SC (AJK)", "SC (A.J.&K.)")
YEAR_FIRST_AJK_PAREN_LABELS = ("Aj&K", "AJ&K", "AJK", "A.J.&K.")


def _ajk_court_token(court: str) -> bool:
    key = re.sub(r"\s+", " ", (court or "").strip())
    folded = re.sub(r"[\s.]", "", key).upper().replace("&", "")
    if folded in ("AJK",):
        return True
    if "AZADJAMMUANDKASHMIR" in folded.replace(" ", ""):
        return True
    return bool(_AJK_TOKEN_RE.match(re.sub(r"\.", "", key).upper()))


def _expand_pld_ajk_hc(citation: str) -> Set[str]:
    m = _PLD_AJK_HC_RE.match(citation.strip())
    if not m or m.group("court").upper().startswith("SC"):
        return set()
    if not _ajk_court_token(m.group("court")):
        return set()
    year, page = m.group("year"), m.group("page")
    return {f"PLD {year} {label} {page}" for label in PLD_AJK_HC_LABELS}


def _expand_pld_ajk_sc(citation: str) -> Set[str]:
    m = _PLD_AJK_SC_RE.match(citation.strip())
    if not m or not _ajk_court_token(m.group("court")):
        return set()
    year, page = m.group("year"), m.group("page")
    return {f"PLD {year} {label} {page}" for label in PLD_AJK_SC_LABELS}


def _expand_year_first_ajk_paren(citation: str) -> Set[str]:
    m = _YEAR_FIRST_PAREN_RE.match(citation.strip())
    if not m or not _ajk_court_token(m.group("court")):
        return set()
    year, reporter, page = m.group("year"), m.group("reporter"), m.group("page")
    return {f"{year} {reporter} ({label}) {page}" for label in YEAR_FIRST_AJK_PAREN_LABELS}


def citation_harvest_equivalent_keys(citation: str) -> Set[str]:
    """All citation strings that should count as the same judgment for search-harvest known checks."""
    keys: Set[str] = set()
    text = (citation or "").strip()
    if not text:
        return keys
    keys.add(text)
    norm = normalise_citation(text)
    if norm:
        keys.add(norm)
    for candidate in list(keys):
        keys |= _expand_pld_ajk_hc(candidate)
        keys |= _expand_pld_ajk_sc(candidate)
        keys |= _expand_year_first_ajk_paren(candidate)
    return keys
