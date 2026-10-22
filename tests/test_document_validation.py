"""B7: document save validation gate."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.document_validation import (
    record_validation_reject,
    validate_judgment_reconciled_json,
    validate_statute_reconciled_json,
)
from scraper.models import CorpusMetadata


def test_validate_statute_rejects_empty_sections():
    ok, reason = validate_statute_reconciled_json({"statute_name": "Foo Act", "sections": []}, raw_text="")
    assert not ok
    assert reason == "empty_statute_text"


def test_validate_statute_rejects_section_rows_without_text():
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "Foo Act", "sections": [{"section_number": "1", "section_text": "   "}]},
        raw_text="x" * 100,
    )
    assert not ok
    assert reason == "empty_statute_sections"


def test_validate_statute_accepts_parseable_section():
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "Foo Act", "sections": [{"section_number": "1", "section_text": "Body text here."}]},
        raw_text="",
    )
    assert ok
    assert reason == ""


def test_validate_judgment_rejects_short_text():
    ok, reason = validate_judgment_reconciled_json({}, raw_text="x" * 100)
    assert not ok
    assert reason == "empty_judgment_text"


def test_validate_judgment_accepts_fixture_length():
    ok, reason = validate_judgment_reconciled_json({}, raw_text="x" * 148)
    assert ok


def test_validation_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "SSA_DOCUMENT_SAVE_VALIDATION", False)
    ok, _ = validate_statute_reconciled_json({"sections": []}, raw_text="")
    assert ok


@pytest.mark.asyncio
async def test_record_validation_reject_increments_counter(db):
    await record_validation_reject(db, kind="statute", reason_code="empty_statute_text", source_name="Test")
    await db.commit()
    row = (
        await db.execute(
            select(CorpusMetadata).where(CorpusMetadata.key == "document_validation_rejects:statute:empty_statute_text")
        )
    ).scalars().first()
    assert row is not None
    assert int(row.value) == 1
    await record_validation_reject(db, kind="statute", reason_code="empty_statute_text", source_name="Test")
    await db.commit()
    row = (
        await db.execute(
            select(CorpusMetadata).where(CorpusMetadata.key == "document_validation_rejects:statute:empty_statute_text")
        )
    ).scalars().first()
    assert int(row.value) == 2
