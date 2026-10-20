"""B4 backfill: order-sheet titles, PLD collisions, headnote strip."""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.models import Citation, Judgment
from scraper.tasks.pls_backfill import (
    backfill_order_sheet_titles,
    backfill_pld_citations,
    requalify_quarantined,
    strip_urdu_headnote_mojibake,
)
from tests.test_order_sheet_title import EXPECTED_HEADER_TITLE, HEADER

TEXT = HEADER + "\nJUDGMENT\nBody text here.\n"


@pytest.mark.asyncio
async def test_order_sheet_title_backfill_dry_run_does_not_write(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    j = Judgment(
        canonical_citation="PLS title dry-run",
        case_title="Wrong title versus other",
        full_text=TEXT,
        full_text_hash="b" * 64,
        source_name="PakistanLawSite",
    )
    db.add(j)
    await db.commit()
    counts = await backfill_order_sheet_titles(db, limit=5, dry_run=True)
    await db.commit()
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["updated"] == 1
    assert refreshed.case_title == "Wrong title versus other"


@pytest.mark.asyncio
async def test_order_sheet_title_backfill_apply_writes_valid_title(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    j = Judgment(
        canonical_citation="PLS title apply",
        case_title="Wrong title versus other",
        full_text=TEXT,
        full_text_hash="c" * 64,
        source_name="PakistanLawSite",
    )
    db.add(j)
    await db.commit()
    counts = await backfill_order_sheet_titles(db, limit=5, dry_run=False)
    await db.commit()
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["updated"] == 1
    assert refreshed.case_title == EXPECTED_HEADER_TITLE


@pytest.mark.asyncio
async def test_pld_backfill_skips_canonical_collision(db):
    holder = Judgment(
        canonical_citation="2020 PLD 999",
        full_text_hash="d" * 64,
        full_text="IN THE LAHORE HIGH COURT, LAHORE\nBefore J\n" + "x" * 600,
        source_name="PakistanLawSite",
    )
    target = Judgment(
        canonical_citation="PLD 2020 Lahore 999",
        full_text_hash="e" * 64,
        full_text="IN THE LAHORE HIGH COURT, LAHORE\nBefore J\n" + "y" * 600,
        source_name="PakistanLawSite",
    )
    db.add(holder)
    db.add(target)
    await db.flush()
    db.add(Citation(judgment_id=holder.id, citation_string="2020 PLD 999", raw_string="2020 PLD 999", is_primary=True))
    await db.commit()
    counts = await backfill_pld_citations(db, limit=10, dry_run=False)
    await db.commit()
    assert counts["collisions"] == 1
    assert counts["updated"] == 0
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == holder.id))).scalars().one()
    assert refreshed.canonical_citation == "2020 PLD 999"
    other = (await db.execute(select(Judgment).where(Judgment.id == target.id))).scalars().one()
    assert other.canonical_citation == "PLD 2020 Lahore 999"


@pytest.mark.asyncio
async def test_strip_headnote_mojibake_apply(db):
    raw = (
        "IN THE HIGH COURT OF SINDH\nThe application is allowed.\nAnnounced.\nJ.\n"
        "???? ???? ????\n"
    )
    j = Judgment(
        canonical_citation="PLS strip headnote",
        full_text=raw,
        full_text_hash="f" * 64,
        source_name="PakistanLawSite",
        quality_flag_urdu_headnote_mojibake=True,
    )
    db.add(j)
    await db.commit()
    counts = await strip_urdu_headnote_mojibake(db, limit=5, dry_run=False)
    await db.commit()
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["stripped"] == 1
    assert "????" not in (refreshed.full_text or "")
    assert refreshed.quality_flag_urdu_headnote_mojibake is False


def test_mutant_m18_requalify_passes_staging_source_name_to_reconcile():
    src = inspect.getsource(requalify_quarantined)
    assert "source_name=st.source_name" in src


def test_backfill_functions_default_dry_run_true():
    import scraper.tasks.pls_backfill as mod

    for name in ("backfill_case_metadata", "requalify_quarantined", "backfill_order_sheet_titles", "backfill_pld_citations"):
        sig = inspect.signature(getattr(mod, name))
        assert sig.parameters["dry_run"].default is True
