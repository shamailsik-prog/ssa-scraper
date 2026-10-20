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
    if re.search(r"azad\s+jammu|jammu\s+and\s+kashmir", low):
        if "supreme court" in low:
            return "SC (AJ&K)"
        return "Aj&K"
    if "supreme court of pakistan" in low or (
        "supreme court" in low and "azad" not in low and "jammu" not in low and "kashmir" not in low
    ):
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
    return None


def _unambiguous_header_token(header_text: Optional[str]) -> Optional[str]:
    """Require agreement across all explicit court captions in the inspected text.

    Unknown captions and contradictory courts are not safe for automatic relabelling.
    """
    tokens = set()
    for line in (header_text or "")[:8000].splitlines():
        label = court_from_judgment_header(line)
        if not label:
            continue
        token = _pld_token_from_header_court(label)
        if not token:
            return None
        tokens.add(token)
        if len(tokens) > 1:
            return None
    return next(iter(tokens)) if len(tokens) == 1 else None


def relabel_pld_citation(citation: Optional[str], header_text: Optional[str]) -> Optional[str]:
    if not citation:
        return citation
    norm = normalise_citation(citation) or citation.strip()
    m = _PLD_MISSING_COURT.match(norm)
    if not m:
        return citation
    year = m.group("year1") or m.group("year2")
    page = m.group("page1") or m.group("page2")
    court = _unambiguous_header_token(header_text)
    if not court:
        return citation
    return f"PLD {year} {court} {page}"
