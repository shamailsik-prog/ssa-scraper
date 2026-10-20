"""Fill missing PLD court tokens in canonical citations from the judgment header line only."""

from __future__ import annotations

import re
from typing import Optional

from scraper.parsers.citation_extractor import normalise_citation
from scraper.parsers.judgment_header_court import court_from_judgment_header

_PLD_MISSING_COURT = re.compile(
    r"^(?:(?P<year1>\d{4})\s+PLD\s+(?P<page1>\d+)|PLD\s+(?P<year2>\d{4})\s+(?P<page2>\d+))$",
    re.I,
)


def _pld_token_from_header_court(header_court: str) -> Optional[str]:
    """Map the IN THE ... COURT header phrase to a PLD reporter court token (SC checked first)."""
    label = re.sub(r"\s+", " ", (header_court or "").strip())
    if not label:
        return None
    upper = label.upper()
    low = label.lower()
    if "SUPREME COURT" in upper and not re.search(r"high\s+court\s+of", low):
        return "SC"
    if "high court of sindh" in low or ("sindh" in low and "high court" in low):
        return "Sindh"
    if "balochistan" in low and "high court" in low:
        return "Balochistan"
    if "peshawar" in low and "high court" in low:
        return "Peshawar"
    if "islamabad" in low and "high court" in low:
        return "Islamabad"
    if "lahore" in low and "high court" in low:
        return "Lahore"
    if "karachi" in low and "high court" in low:
        return "Karachi"
    if "SUPREME COURT OF PAKISTAN" in upper:
        return "SC"
    return None


def relabel_pld_citation(citation: Optional[str], header_text: Optional[str]) -> Optional[str]:
    if not citation:
        return citation
    norm = normalise_citation(citation) or citation.strip()
    m = _PLD_MISSING_COURT.match(norm)
    if not m:
        return citation
    year = m.group("year1") or m.group("year2")
    page = m.group("page1") or m.group("page2")
    header_court = court_from_judgment_header(header_text)
    if not header_court:
        return citation
    court = _pld_token_from_header_court(header_court)
    if not court:
        return citation
    return f"PLD {year} {court} {page}"
