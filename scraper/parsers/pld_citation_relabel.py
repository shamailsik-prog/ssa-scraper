"""Fill missing PLD court tokens in canonical citations from the judgment header."""

from __future__ import annotations

import re
from typing import Optional

from scraper.parsers.citation_extractor import normalise_citation

_PLD_MISSING_COURT = re.compile(
    r"^(?:(?P<year1>\d{4})\s+PLD\s+(?P<page1>\d+)|PLD\s+(?P<year2>\d{4})\s+(?P<page2>\d+))$",
    re.I,
)
_COURT_TOKENS = (
    ("lahore", "Lahore"),
    ("karachi", "Karachi"),
    ("peshawar", "Peshawar"),
    ("quetta", "Balochistan"),
    ("islamabad", "Islamabad"),
    ("supreme court of pakistan", "SC"),
)


def relabel_pld_citation(citation: Optional[str], header_text: Optional[str]) -> Optional[str]:
    if not citation:
        return citation
    norm = normalise_citation(citation) or citation.strip()
    m = _PLD_MISSING_COURT.match(norm)
    if not m:
        return citation
    year = m.group("year1") or m.group("year2")
    page = m.group("page1") or m.group("page2")
    head = (header_text or "")[:4000].lower()
    court = None
    for key, token in _COURT_TOKENS:
        if key in head:
            court = token
            break
    if not court:
        return citation
    return f"PLD {year} {court} {page}"
