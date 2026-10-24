"""Deterministic per-record verification rules (no I/O)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from scraper.judgment_court_consistency import judgment_court_fields_consistent
from scraper.models import Court, Judgment, Statute
from scraper.parsers.assembly_pdf_sections import is_table_of_contents_chunk, total_section_body_chars
from scraper.parsers.citation_extractor import normalise_citation
from scraper.parsers.pls_text_encoding import classify_mojibake
from scraper.pls_quality_guards import SHORT_TEXT_MIN_CHARS
from scraper.extractors.judgment_guards import (
    _BODY_ORDER_MARKER_RE,
    detect_headnote_only_no_body,
    strip_leading_judgment_chrome,
)

RULES_VERSION = "2026-10-24.1"

_PAGE_CHROME_RE = re.compile(
    r"(?im)^\s*(?:page\s+\d+\s+of\s+\d+|^\s*\d+\s*$|update\s+subscriber|obtaining\s+subscription)\b",
)


@dataclass(frozen=True)
class VerificationOutcome:
    verdict: str
    reason_codes: List[str]
    journal_year: Optional[int]
    canonical_key: Optional[str]


def _norm_citation_key(citation: Optional[str]) -> str:
    if not citation:
        return ""
    return re.sub(r"[\s.]+", "", (normalise_citation(citation) or citation).upper())


def _header_citation_key(full_text: Optional[str]) -> str:
    if not full_text:
        return ""
    first = full_text.strip().splitlines()[0] if full_text.strip() else ""
    return _norm_citation_key(first)


def _year_in_text(year: Optional[int], text: Optional[str]) -> bool:
    if year is None or not text:
        return year is None
    return str(year) in text[:800]


def _court_in_text(court_name: Optional[str], text: Optional[str]) -> bool:
    if not court_name or not text:
        return not court_name
    blob = text[:1200].lower()
    tokens = [t for t in re.split(r"[\s\-]+", court_name.lower()) if len(t) > 2]
    if not tokens:
        return True
    hits = sum(1 for t in tokens[:6] if t in blob)
    return hits >= min(2, len(tokens))


def verify_judgment_row(
    j: Judgment,
    *,
    court_row: Optional[Court],
    duplicate_citations: Set[str],
    duplicate_hashes: Set[str],
) -> VerificationOutcome:
    reasons: List[str] = []
    text = j.full_text or ""
    chrome_stripped = strip_leading_judgment_chrome(text)
    canonical = j.canonical_citation or ""
    header_key = _header_citation_key(text)
    stored_key = _norm_citation_key(canonical)
    if stored_key and header_key and header_key != stored_key:
        reasons.append("header_citation_mismatch")
    if not _BODY_ORDER_MARKER_RE.search(chrome_stripped):
        reasons.append("body_marker_missing")
    if len(chrome_stripped.strip()) < SHORT_TEXT_MIN_CHARS:
        reasons.append("body_too_short")
    if detect_headnote_only_no_body(text) is not None:
        reasons.append("body_marker_missing")
    if j.court_name and not _court_in_text(j.court_name, text):
        reasons.append("court_label_mismatch")
    if j.year is not None and not _year_in_text(j.year, text):
        reasons.append("year_label_mismatch")
    if j.court_id is not None and not judgment_court_fields_consistent(
        court_name=j.court_name,
        court_id=j.court_id,
        court_row=court_row,
    ):
        reasons.append("court_id_name_mismatch")
    moj = classify_mojibake(text)
    if moj == "body":
        reasons.append("mojibake_body")
    elif moj == "headnote":
        reasons.append("mojibake_headnote")
    if _PAGE_CHROME_RE.search(text[:2000]):
        reasons.append("page_chrome_junk")
    if canonical and canonical in duplicate_citations:
        reasons.append("duplicate_citation")
    if j.full_text_hash and j.full_text_hash in duplicate_hashes:
        reasons.append("duplicate_text_hash")
    verdict = "PASS" if not reasons else "FAIL"
    return VerificationOutcome(
        verdict=verdict,
        reason_codes=sorted(set(reasons)),
        journal_year=j.year,
        canonical_key=canonical[:500] if canonical else None,
    )


def verify_statute_row(
    statute: Statute,
    sections: Sequence[Dict[str, str]],
    *,
    duplicate_names: Set[str],
) -> VerificationOutcome:
    reasons: List[str] = []
    name_key = (statute.name or "").strip().lower()
    if name_key and name_key in duplicate_names:
        reasons.append("duplicate_statute_name")
    if not sections:
        reasons.append("statute_sections_empty")
    else:
        for sec in sections:
            body = (sec.get("section_text") or "").strip()
            if is_table_of_contents_chunk(body):
                reasons.append("statute_section_toc_noise")
        if total_section_body_chars(sections) < 80:
            reasons.append("statute_sections_empty")
    verdict = "PASS" if not reasons else "FAIL"
    year = statute.year_enacted
    key = (statute.name or "")[:500]
    return VerificationOutcome(
        verdict=verdict,
        reason_codes=sorted(set(reasons)),
        journal_year=year,
        canonical_key=key or None,
    )
