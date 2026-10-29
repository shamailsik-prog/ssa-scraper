"""Validation gates for corpus document saves (B7). Refuses empty statute/judgment payloads before promotion."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from scraper.config import settings
from scraper.statute_image_gate import is_binary_garbage_text
from scraper.statute_quality.ocr_section_mash import VERDICT_MISMATCH, classify_ocr_section_mash

logger = logging.getLogger(__name__)


def _norm_section(num: Any) -> str:
    return str(num or "").strip()


def _parseable_sections(sections: list) -> list:
    out = []
    for s in sections or []:
        if not isinstance(s, dict):
            continue
        num = _norm_section(s.get("section_number"))
        txt = (s.get("section_text") or "").strip()
        if num and txt:
            out.append(s)
    return out

_REJECT_COUNTER = "document_validation_rejects"


def _enabled() -> bool:
    return bool(settings.SSA_DOCUMENT_SAVE_VALIDATION)


def validate_statute_reconciled_json(
    data: Optional[Dict[str, Any]],
    *,
    raw_text: str = "",
    source_name: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Tuple[bool, str]:
    """Return (ok, reason_code)."""
    if not _enabled():
        return True, ""
    payload = data or {}
    blobs = [str(payload.get("statute_name") or ""), raw_text or ""]
    for section in payload.get("sections") or []:
        if isinstance(section, dict):
            blobs.append(str(section.get("section_text") or ""))
    if any(is_binary_garbage_text(blob) for blob in blobs):
        return False, "binary_statute_text"
    sections = payload.get("sections") or []
    dict_sections = [s for s in sections if isinstance(s, dict)]
    parseable = _parseable_sections(sections)
    text = (raw_text or "").strip()
    if not parseable:
        if sections:
            return False, "empty_statute_sections"
        if not text or len(text) < 80:
            return False, "empty_statute_text"
        return False, "empty_statute_sections"
    if dict_sections and len(parseable) < len(dict_sections):
        return False, "empty_statute_sections"
    from scraper.statute_quality.distribution_list import is_distribution_addressee_section
    from scraper.statute_quality.senate_integrity import name_s1_mismatch, section_embeds_next_act

    if any(is_distribution_addressee_section(str(s.get("section_text") or "")) for s in parseable):
        return False, "distribution_list_footer_as_sections"
    if name_s1_mismatch(str(payload.get("statute_name") or ""), parseable):
        return False, "name_s1_mismatch"
    if any(section_embeds_next_act(str(s.get("section_text") or "")) for s in parseable):
        return False, "bundled_gazette_act_bleed"
    verdict = classify_ocr_section_mash(
        parseable,
        source_url=source_url or str(payload.get("source_url") or ""),
        source_name=source_name or str(payload.get("source_name") or ""),
        scanned_source=None,
    )
    if verdict.verdict == VERDICT_MISMATCH:
        return False, verdict.reason
    return True, ""


def validate_judgment_reconciled_json(data: Optional[Dict[str, Any]], *, raw_text: str = "") -> Tuple[bool, str]:
    if not _enabled():
        return True, ""
    body = (raw_text or "").strip()
    if data:
        body = (data.get("full_text") or data.get("judgment_text") or body or "").strip()
    # Floor below SSA_SHORT_TEXT_GUARD (500); after strip_leading_judgment_chrome corpus_chain fixtures ~148 chars.
    if len(body) < 140:
        return False, "empty_judgment_text"
    return True, ""


async def record_validation_reject(db, *, kind: str, reason_code: str, source_name: Optional[str] = None) -> None:
    """Log and bump a corpus metadata counter (no silent drop)."""
    from sqlalchemy import select

    from scraper.models import CorpusMetadata

    logger.warning(
        "document_validation_reject kind=%s reason=%s source=%s",
        kind,
        reason_code,
        source_name or "",
    )
    key = f"{_REJECT_COUNTER}:{kind}:{reason_code}"
    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == key))).scalars().first()
    if row is None:
        db.add(CorpusMetadata(key=key, value="1"))
    else:
        try:
            row.value = str(int(row.value or 0) + 1)
        except (TypeError, ValueError):
            row.value = "1"
