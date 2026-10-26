"""Re-extract act title and year from Senate / AJK gazette PDF headers."""

from __future__ import annotations

import re
from typing import Optional, Tuple

from scraper.parsers.citation_extractor import canonicalise_statute_name

YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
MAY_BE_CALLED_RE = re.compile(
    r"(?is)\b(?:this|the)\s+act\s+may\s+be\s+called\s+the\s+"
    r"(?P<title>[^.;:\n]{6,200}?\b(?:act|ordinance|order|rules|regulations)\b[^.;:\n]{0,40})"
)
ACT_NO_OF_YEAR_RE = re.compile(
    r"(?im)^\s*(?:the\s+)?(?P<title>[^.\n]{6,220}?\b(?:act|ordinance|order|rules|regulations)\b[^.\n]{0,40})"
    r",?\s*(?:\(act\s+no\.?\s*[^)]+\)\s*)?(?:of\s+)?(?P<year>(?:1[89]|20)\d{2})\s*\.?\s*$"
)
ACT_NO_LINE_RE = re.compile(
    r"(?im)^\s*act\s+no\.?\s*(?:[IVXLC]+|\d+)\s+of\s+(?P<year>(?:1[89]|20)\d{2})\b"
)
SKIP_LINE_RE = re.compile(
    r"(?i)\b("
    r"the\s+following\s+act|majlis-?e-?shoora|parliament\s+has\s+passed|"
    r"received\s+the\s+assent|assented\s+to|published\s+in\s+the\s+gazette|"
    r"it\s+is\s+hereby\s+notified|sanctioned\s+by\s+the\s+president"
    r")\b"
)
REJECT_TITLE_PREFIX_RE = re.compile(r"(?i)^the\s+following\b")

SENATE_SOURCES = frozenset({"Senate"})
AJK_SOURCES = frozenset({"AJKAssembly"})
NA_SOURCES = frozenset({"NationalAssembly"})


def _normalize_title(raw: str) -> str:
    line = re.sub(r"\s+", " ", (raw or "").strip(" ."))
    if REJECT_TITLE_PREFIX_RE.search(line):
        return ""
    if line.lower().startswith("the "):
        return line[0].upper() + line[1:]
    return line


def _year_from_title(title: str) -> Optional[int]:
    m = YEAR_RE.search(title or "")
    return int(m.group(1)) if m else None


def extract_gazette_title_year(text: str, *, source_name: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
    if not text or source_name not in (SENATE_SOURCES | AJK_SOURCES | NA_SOURCES):
        return None
    head = text[:16000]
    title_raw: Optional[str] = None
    year: Optional[int] = None

    m = MAY_BE_CALLED_RE.search(head)
    if m:
        title_raw = m.group("title")
        year = _year_from_title(title_raw)

    if not title_raw:
        for line in head.splitlines()[:120]:
            if not line.strip() or SKIP_LINE_RE.search(line):
                continue
            m2 = ACT_NO_OF_YEAR_RE.match(line.strip())
            if m2:
                title_raw = m2.group("title")
                year = int(m2.group("year"))
                break

    if not title_raw:
        act_no = ACT_NO_LINE_RE.search(head)
        if act_no:
            year = int(act_no.group("year"))
            for line in head.splitlines()[:120]:
                if SKIP_LINE_RE.search(line):
                    continue
                if re.search(r"(?i)\bact\b", line) and YEAR_RE.search(line):
                    m3 = ACT_NO_OF_YEAR_RE.match(line.strip())
                    if m3:
                        title_raw = m3.group("title")
                        break

    if not title_raw:
        return None
    title = _normalize_title(title_raw)
    if not title or not re.search(r"(?i)\b(act|ordinance|order|rules|regulations)\b", title):
        return None
    if year is None:
        year = _year_from_title(title)
    canonical = canonicalise_statute_name(title, year) or title
    return canonical, year
