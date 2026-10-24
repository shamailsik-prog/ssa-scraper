"""Single judgment minimum-body threshold (500 when unified flag is on)."""

from __future__ import annotations

from scraper.pls_quality_guards import SHORT_TEXT_MIN_CHARS

LEGACY_DOCUMENT_VALIDATION_MIN_CHARS = 140


def judgment_min_body_chars() -> int:
    from scraper.config import settings

    if bool(getattr(settings, "SSA_UNIFIED_JUDGMENT_MIN_BODY", False)):
        return SHORT_TEXT_MIN_CHARS
    return LEGACY_DOCUMENT_VALIDATION_MIN_CHARS
