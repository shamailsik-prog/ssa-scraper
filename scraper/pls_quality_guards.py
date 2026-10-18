"""Optional PakistanLawSite quality guards (feature-flagged)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from scraper.config import settings
from scraper.parsers.judgment_header_court import court_from_judgment_header
from scraper.parsers.pld_citation_relabel import relabel_pld_citation
from scraper.parsers.pls_text_encoding import has_mojibake_run

SHORT_TEXT_MIN_CHARS = 500


def apply_pls_quality_guards(
    out: Dict[str, Any],
    *,
    raw_text: str,
    source_name: Optional[str],
    errors: List[str],
) -> None:
    """Mutate ``out`` and append to ``errors`` when flags are on. No-op when flags are off."""
    if source_name != "PakistanLawSite":
        return
    if settings.SSA_ENCODING_GUARD and has_mojibake_run(raw_text):
        errors.append("preserved text contains mojibake (????) run; re-fetch required")
    if settings.SSA_SHORT_TEXT_GUARD and len((raw_text or "").strip()) < SHORT_TEXT_MIN_CHARS:
        errors.append(f"judgment text shorter than {SHORT_TEXT_MIN_CHARS} characters")
    if settings.SSA_COURT_HEADER_GUARD:
        header_court = court_from_judgment_header(raw_text)
        stored = out.get("court")
        if header_court and stored and header_court.lower() != str(stored).lower():
            if "supreme court" in str(stored).lower() and "high court" in header_court.lower():
                errors.append(f"court mismatch: stored={stored!r} header={header_court!r}")
    if settings.SSA_PLD_CITATION_RELABEL:
        cits = list(out.get("citations") or [])
        if cits:
            relabelled = relabel_pld_citation(cits[0], raw_text)
            if relabelled and relabelled != cits[0]:
                cits[0] = relabelled
                out["citations"] = cits
