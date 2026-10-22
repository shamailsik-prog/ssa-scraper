"""Validation gates for corpus document saves (B7). Refuses empty statute/judgment payloads before promotion."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from scraper.config import settings

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


def validate_statute_reconciled_json(data: Optional[Dict[str, Any]], *, raw_text: str = "") -> Tuple[bool, str]:
    """Return (ok, reason_code)."""
    if not _enabled():
        return True, ""
    payload = data or {}
    name = (payload.get("statute_name") or "").strip()
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
