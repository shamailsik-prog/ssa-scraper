"""Detect the judgment's own citation from preserved header text (re-key / guard)."""

from __future__ import annotations

import re
from typing import Optional, Tuple

from scraper.extractors.deterministic import citation_referred_in_text
from scraper.parsers.case_metadata import judgment_caption_slice
from scraper.parsers.citation_extractor import extract_citations, normalise_citation

_ANCHOR_AFTER_CITE = re.compile(
    r"^\s*(?:\[[^\]]+\]|Before\b|Present\.?)",
    re.IGNORECASE,
)


def citation_key(raw: Optional[str]) -> str:
    return normalise_citation(raw or "")


def _first_own_citation_before(text: str, end: int) -> Optional[Tuple[str, int, int]]:
    raw = text or ""
    for c in extract_citations(raw):
        if c["span"][0] >= end:
            break
        if citation_referred_in_text(raw, c["span"][0], c["span"][1]):
            continue
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if norm:
            return norm, c["span"][0], c["span"][1]
    return None


def citation_followed_by_header_anchor(text: str, span_end: int) -> bool:
    tail = (text or "")[span_end : span_end + 120]
    return _ANCHOR_AFTER_CITE.match(tail) is not None


def leading_header_citation_judgment(text: Optional[str]) -> Optional[str]:
    """Leading own citation in the judgment caption/header block."""
    raw = text or ""
    if not raw:
        return None
    end = len(judgment_caption_slice(raw))
    hit = _first_own_citation_before(raw, end)
    return hit[0] if hit else None


def leading_header_citation_staging(text: Optional[str]) -> Optional[str]:
    """First citation in staging text that is followed by [court], Before, or Present."""
    raw = text or ""
    if not raw:
        return None
    scan_limit = min(len(raw), max(2500, int(len(raw) * 0.12)))
    for c in extract_citations(raw[:scan_limit]):
        if citation_referred_in_text(raw, c["span"][0], c["span"][1]):
            continue
        if not citation_followed_by_header_anchor(raw, c["span"][1]):
            continue
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if norm:
            return norm
    return None


def _cite_parts(norm: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    parts = (norm or "").split()
    if len(parts) < 3:
        return None, None, None
    if parts[0].isdigit():
        return parts[1], int(parts[0]), parts[-1]
    if parts[0] == "PLD" and parts[1].isdigit():
        return "PLD", int(parts[1]), parts[-1]
    if parts[1].isdigit():
        return parts[0], int(parts[1]), parts[-1]
    return parts[0], None, parts[-1]


def mismatch_kind(stored: str, header: str) -> Optional[str]:
    """page_only | reporter_year | None when keys already match."""
    s = citation_key(stored)
    h = citation_key(header)
    if not s or not h or s == h:
        return None
    sr, sy, sp = _cite_parts(s)
    hr, hy, hp = _cite_parts(h)
    if sr and hr and sy is not None and hy is not None and sr == hr and sy == hy and sp != hp:
        return "page_only"
    return "reporter_year"


def header_citation_anchor_for_key(text: str, header_key: str) -> bool:
    raw = text or ""
    end = len(judgment_caption_slice(raw))
    for c in extract_citations(raw):
        if c["span"][0] >= end:
            break
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if norm != citation_key(header_key):
            continue
        return citation_followed_by_header_anchor(raw, c["span"][1])
    return False


def rekey_actionable(
    stored: str,
    header: str,
    text: str,
    *,
    staging: bool = False,
) -> bool:
    kind = mismatch_kind(stored, header)
    if kind is None:
        return False
    if kind == "page_only":
        return True
    if staging:
        return True
    return header_citation_anchor_for_key(text, header)
