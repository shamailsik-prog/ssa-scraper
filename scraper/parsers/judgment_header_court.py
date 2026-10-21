"""Court name from the judgment header line (In the ... Court), not scrape defaults."""

from __future__ import annotations

import re
from typing import Optional

_HEADER_COURT = re.compile(
    r"(?im)^\s*in\s+the\s+("
    r"high\s+court\s+of\s+[^\n,]+|"
    r"[A-Z][^\n]{5,120}?\bcourt\b[^\n]{0,40}"
    r")",
)
_SC_LABEL = re.compile(r"(?i)supreme\s+court\s+of\s+pakistan")
_HC_LABEL = re.compile(r"(?i)high\s+court")


def court_from_judgment_header(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    head = (text or "")[:8000]
    m = _HEADER_COURT.search(head)
    if not m:
        return None
    label = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")
    if _SC_LABEL.search(label) and _HC_LABEL.search(head[: m.start()] + head[m.end() : m.end() + 400]):
        # Header names SC but body is clearly HC — prefer the header phrase as written for manual review
        return label[:255]
    return label[:255] or None
