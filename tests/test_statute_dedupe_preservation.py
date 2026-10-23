"""Fail-closed statute deduplication: a title match must not hide unique law."""
from __future__ import annotations

from uuid import uuid4

import pytest

from scraper.models import Statute, StatuteSection
from scraper.statute_quality.dedupe import can_quarantine_subsumed_statute_sections
from scraper.tasks.statute_quality import _sections_map_for_statutes, find_duplicate_groups
from tests.test_statute_quality_cli import _add_section


def test_substantive_amendment_is_not_a_duplicate():
    keeper = [{"section_number": "1", "section_text": "Section 1. Original entitlement."}]
    amended = [{"section_number": "1", "section_text": "Section 1. New entitlement."}]
    assert not can_quarantine_subsumed_statute_sections(keeper, amended)


def test_unique_letter_suffix_section_is_preserved():
    keeper = [{"section_number": "1", "section_text": "Section 1. Existing."}]
    candidate = keeper + [{"section_number": "1A", "section_text": "Section 1A. New right."}]
    assert not can_quarantine_subsumed_statute_sections(keeper, candidate)


def test_exact_subsumed_text_is_safe_even_with_whitespace_variation():
    keeper = [
        {"section_number": "1", "section_text": "Section 1. Same text."},
        {"section_number": "2", "section_text": "Section 2. Extra text."},
    ]
    candidate = [{"section_number": "1", "section_text": "Section 1. Same  text.\n"}]
    assert can_quarantine_subsumed_statute_sections(keeper, candidate)


@pytest.mark.parametrize("candidate", [
    [],
    [{"section_number": "1", "section_text": ""}],
    [{"section_number": "1", "section_text": "A"}, {"section_number": "1", "section_text": "B"}],
])
def test_unknown_or_ambiguous_content_is_never_quarantined(candidate):
    assert not can_quarantine_subsumed_statute_sections(
        [{"section_number": "1", "section_text": "A"}], candidate
    )


@pytest.mark.asyncio
async def test_db_same_title_distinct_section_is_review_only(db):
    winner, distinct = uuid4(), uuid4()
    for sid, name in (
        (winner, "Sample Rights Act, 2020"),
        (distinct, "Sample Rights Act,2020"),
    ):
        db.add(Statute(
            id=sid, name=name, year_enacted=2020, jurisdiction="Federal",
            source_name="Senate", source_url="https://example.test/statute",
        ))
    await db.commit()
    await _add_section(db, winner, "1", "Section 1. Same base right.")
    await _add_section(db, winner, "2", "Section 2. Another section.")
    await _add_section(db, distinct, "1", "Section 1. Changed substantive right.")
    await db.commit()
    result = await find_duplicate_groups(db)
    assert result["duplicate_groups"] == 1
    assert result["statutes_to_quarantine"] == 0
    assert result["review_required_ids"] == [distinct]


@pytest.mark.asyncio
async def test_db_exact_subset_can_be_quarantined(db):
    winner, subset = uuid4(), uuid4()
    for sid, name in (
        (winner, "Sample Duplicate Act, 2021"),
        (subset, "Sample Duplicate Act,2021"),
    ):
        db.add(Statute(
            id=sid, name=name, year_enacted=2021, jurisdiction="Federal",
            source_name="Senate", source_url="https://example.test/statute",
        ))
    await db.commit()
    await _add_section(db, winner, "1", "Section 1. Same exact right.")
    await _add_section(db, winner, "2", "Section 2. Another right.")
    await _add_section(db, subset, "1", "Section 1. Same exact right.")
    await db.commit()
    result = await find_duplicate_groups(db)
    assert result["statutes_to_quarantine"] == 1
    assert result["losers"] == [subset]
    assert result["review_required"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable_kind", ["empty_text", "missing_version"])
async def test_db_unreadable_unique_section_never_auto_quarantined(db, unreadable_kind):
    """A unique section must not vanish from comparison merely because OCR/versioning failed."""
    keeper, candidate = uuid4(), uuid4()
    for sid, name in (
        (keeper, "Unreadable Section Act, 2024"),
        (candidate, "Unreadable Section Act,2024"),
    ):
        db.add(Statute(
            id=sid, name=name, year_enacted=2024, jurisdiction="Federal",
            source_name="Senate", source_url="https://example.test/statute",
        ))
    await db.commit()
    await _add_section(db, keeper, "1", "Section 1. Existing right.")
    await _add_section(db, keeper, "2", "Section 2. Another substantive right.")
    await _add_section(db, candidate, "1", "Section 1. Existing right.")
    if unreadable_kind == "empty_text":
        await _add_section(db, candidate, "1A", "")
    else:
        db.add(StatuteSection(statute_id=candidate, section_number="1A"))
    await db.commit()

    mapped = await _sections_map_for_statutes(db, [keeper, candidate])
    assert len(mapped[candidate]) == 2, "unreadable sections must remain in comparison"
    assert any(s["section_number"] == "1A" and not s["section_text"] for s in mapped[candidate])

    result = await find_duplicate_groups(db)
    assert result["duplicate_groups"] == 1
    assert result["statutes_to_quarantine"] == 0
    assert result["review_required_ids"] == [candidate]
