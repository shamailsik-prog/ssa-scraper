"""B3: statute promotion gates (listing-only, no year, duplicate name, no delete)."""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import func, select

from scraper.fetchers import record_provenance, stage_statute
from scraper.models import ScraperSource, Statute
from scraper.tasks.promotion import promote_statute_staging


async def _make_statute_staging(db, reconciled_json):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "Senate"))).scalars().first()
    assert source is not None
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/b3-statute.txt",
        content=b"b3",
        content_kind="text",
    )
    st = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text="body",
        url="http://127.0.0.1/b3-statute.txt",
        kind="statute",
    )
    st.status = "extracted"
    st.reconciled_json = reconciled_json
    return st


@pytest.mark.asyncio
async def test_listing_only_staging_quarantined_not_promoted(db):
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Sample Act, 2024",
            "year_enacted": 2024,
            "jurisdiction": "Federal",
            "sections": [],
            "field_evidence": {"listing_only": True},
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert "listing_only_statute" in (st.quarantine_reason or "")
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0


@pytest.mark.asyncio
async def test_no_year_quarantined_by_default(db):
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Sample Compliance Act",
            "jurisdiction": "Federal",
            "sections": [
                {"section_number": "1", "section_title": "Short title", "section_text": "1. This is the Sample Compliance Act."}
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert "statute_no_year" in (st.quarantine_reason or "")


@pytest.mark.asyncio
async def test_duplicate_name_quarantined_by_default(db):
    db.add(Statute(name="Other Sample Act", jurisdiction="Federal", year_enacted=2020))
    await db.flush()
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "other sample act",
            "year_enacted": 2024,
            "jurisdiction": "Federal",
            "sections": [
                {"section_number": "1", "section_title": "Short title", "section_text": "1. Another act with the same canonical name."}
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert "statute_duplicate_name" in (st.quarantine_reason or "")


@pytest.mark.asyncio
async def test_empty_sections_does_not_delete_new_statute_row(db):
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Empty Sections Act, 2026",
            "year_enacted": 2026,
            "jurisdiction": "Federal",
            "sections": [{"section_number": "1", "section_title": "x", "section_text": ""}],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0


@pytest.mark.asyncio
async def test_existing_statute_marked_needs_refetch_on_empty_repromote(db):
    statute = Statute(name="Legacy Stub Act", jurisdiction="Federal", needs_section_refetch=False)
    db.add(statute)
    await db.flush()
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Legacy Stub Act",
            "year_enacted": 1999,
            "jurisdiction": "Federal",
            "sections": [{"section_number": "1", "section_title": "x", "section_text": "   "}],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    await db.refresh(statute)
    assert statute.needs_section_refetch is True


def test_mutant_b3_m1_promote_statute_staging_deletes_only_new_empty_orphan():
    from scraper.tasks import promotion as promotion_mod

    src = inspect.getsource(promotion_mod.promote_statute_staging)
    assert "if created_new_statute and statute is not None:" in src
    assert "await db.delete(statute)" in src


