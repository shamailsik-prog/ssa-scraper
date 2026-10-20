"""Conservatively fill missing PLD court tokens using explicit court headings only."""

from __future__ import annotations

import re
from typing import Optional

from scraper.parsers.citation_extractor import normalise_citation

_PLD_MISSING_COURT = re.compile(
    r"^(?:(?P<year1>\d{4})\s+PLD\s+(?P<page1>\d+)|PLD\s+(?P<year2>\d{4})\s+(?P<page2>\d+))$",
    re.I,
)

# A city mentioned in a judgment is not evidence of the deciding court.
# Only recognize complete court names at the start of a header line.
_COURT_HEADING_PATTERNS = (
    ("SC", re.compile(r"^(?:IN\s+THE\s+)?SUPREME\s+COURT\s+OF\s+PAKISTAN\b(?:\s*(?:$|[,(:\-–]|AT\b))", re.I)),
    ("Lahore", re.compile(r"^(?:IN\s+THE\s+)?(?:LAHORE\s+HIGH\s+COURT|HIGH\s+COURT\s+OF\s+LAHORE)\b(?:\s*(?:$|[,(:\-–]|AT\b))", re.I)),
    ("Karachi", re.compile(r"^(?:IN\s+THE\s+)?(?:SINDH\s+HIGH\s+COURT|HIGH\s+COURT\s+OF\s+SINDH)\b(?:\s*(?:$|[,(:\-–]|AT\b))", re.I)),
    ("Peshawar", re.compile(r"^(?:IN\s+THE\s+)?(?:PESHAWAR\s+HIGH\s+COURT|HIGH\s+COURT\s+OF\s+PESHAWAR)\b(?:\s*(?:$|[,(:\-–]|AT\b))", re.I)),
    ("Balochistan", re.compile(r"^(?:IN\s+THE\s+)?(?:BALOCHISTAN\s+HIGH\s+COURT|HIGH\s+COURT\s+OF\s+BALOCHISTAN)\b(?:\s*(?:$|[,(:\-–]|AT\b))", re.I)),
    ("Islamabad", re.compile(r"^(?:IN\s+THE\s+)?(?:ISLAMABAD\s+HIGH\s+COURT|HIGH\s+COURT\s+OF\s+ISLAMABAD)\b(?:\s*(?:$|[,(:\-–]|AT\b))", re.I)),
)


def _court_from_heading(header_text: Optional[str]) -> Optional[str]:
    # Header extraction may contain excerpts from other courts. Conflicting
    # explicit headings are ambiguous and must never trigger a relabel.
    lines = [line.strip() for line in (header_text or "")[:1200].splitlines() if line.strip()][:12]
    courts = set()
    for line in lines:
        for token, pattern in _COURT_HEADING_PATTERNS:
            if pattern.match(line):
                courts.add(token)
    return next(iter(courts)) if len(courts) == 1 else None


def relabel_pld_citation(citation: Optional[str], header_text: Optional[str]) -> Optional[str]:
    if not citation:
        return citation
    norm = normalise_citation(citation) or citation.strip()
    m = _PLD_MISSING_COURT.match(norm)
    if not m:
        return citation
    court = _court_from_heading(header_text)
    if not court:
        return citation
    year = m.group("year1") or m.group("year2")
    page = m.group("page1") or m.group("page2")
    return f"PLD {year} {court} {page}"
