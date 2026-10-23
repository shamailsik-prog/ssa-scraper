"""Re-extract act title and year from Senate / AJK gazette PDF headers."""

from __future__ import annotations

import re
from typing import Optional, Tuple

from scraper.parsers.citation_extractor import canonicalise_statute_name

GAZETTE_PAKISTAN_HEAD_RE = re.compile(
    r"(?is)\bgazette\s+of\s+pakistan\b.{0,400}?"
    r"(?P<title>the\s+[a-z0-9][^\n]{8,200}?\b(?:act|ordinance|order|rules|regulations)\b[^\n]{0,40})"
)
GAZETTE_AJK_HEAD_RE = re.compile(
    r"(?is)\b(?:azad\s+government|government\s+of\s+the\s+state)\b.{0,500}?"
    r"(?P<title>the\s+[a-z0-9][^\n]{8,200}?\b(?:act|ordinance|order|rules|regulations)\b[^\n]{0,40})"
)
YEAR_IN_TITLE_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
ACT_LINE_RE = re.compile(
    r"(?im)^\s*(the\s+[a-z0-9][^\n]{8,220}?\b(?:act|ordinance|order|rules|regulations)\b[^\n]{0,60})\s*$"
)

SENATE_SOURCES = frozenset({"Senate"})
AJK_SOURCES = frozenset({"AJKAssembly"})


def _normalize_title(raw: str) -> str:
    line = re.sub(r"\s+", " ", (raw or "").strip(" ."))
    if line.lower().startswith("the "):
        return line[0].upper() + line[1:]
    return line


def extract_gazette_title_year(text: str, *, source_name: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
    if not text or not source_name:
        return None
    head = text[:12000]
    title_raw: Optional[str] = None
    if source_name in SENATE_SOURCES:
        m = GAZETTE_PAKISTAN_HEAD_RE.search(head)
        if m:
            title_raw = m.group("title")
    elif source_name in AJK_SOURCES:
        m = GAZETTE_AJK_HEAD_RE.search(head)
        if m:
            title_raw = m.group("title")
    if not title_raw:
        for line in head.splitlines()[:80]:
            if re.search(r"(?i)\bgazette\b", line):
                continue
            m2 = ACT_LINE_RE.match(line)
            if m2:
                title_raw = m2.group(1)
                break
    if not title_raw:
        return None
    title = _normalize_title(title_raw)
    year = None
    ym = YEAR_IN_TITLE_RE.search(title)
    if ym:
        year = int(ym.group(1))
    canonical = canonicalise_statute_name(title, year) or title
    return canonical, year
