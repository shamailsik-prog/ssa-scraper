"""B7: quarantine_bad_documents CLI criteria and apply/undo on test DB."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from scraper.models import Judgment, Statute
from scraper.tasks.quarantine_bad_documents import (
    JUDGMENT_QUARANTINE_REASON,
    STATUTE_QUARANTINE_REASON,
    bad_document_counts,
    run_quarantine_bad_documents,
    undo_quarantine,
)


@pytest.mark.asyncio
async def test_bad_document_counts_statute_without_sections(db):
    db.add(Statute(name="Empty Act", jurisdiction="Federal", source_name="Test"))
    await db.commit()
    counts = await bad_document_counts(db)
    assert counts["statute_bad_rows"] >= 1


@pytest.mark.asyncio
async def test_quarantine_apply_and_undo(db):
    st = Statute(name="Quarantine Me Act", jurisdiction="Federal", source_name="Test")
    db.add(st)
    jg = Judgment(
        canonical_citation="2020 TEST 1",
        access_method="public",
        full_text="x",
        source_name="Test",
    )
    db.add(jg)
    await db.commit()

    applied = await run_quarantine_bad_documents(apply=True)
    assert applied["applied"]["statutes_updated"] >= 1
    assert applied["applied"]["judgments_updated"] >= 1

    from scraper.database import SessionLocal

    async with SessionLocal() as verify:
        st_row = (await verify.execute(select(Statute).where(Statute.name == "Quarantine Me Act"))).scalars().first()
        jg_row = (
            await verify.execute(select(Judgment).where(Judgment.canonical_citation == "2020 TEST 1"))
        ).scalars().first()
        assert st_row is not None
        assert st_row.corpus_quarantined is True
        assert st_row.corpus_quarantine_reason == STATUTE_QUARANTINE_REASON
        assert jg_row is not None
        assert jg_row.corpus_quarantined is True
        assert jg_row.corpus_quarantine_reason == JUDGMENT_QUARANTINE_REASON

        undone = await undo_quarantine(verify)
        assert undone["statutes_restored"] >= 1
        assert undone["judgments_restored"] >= 1

        await verify.refresh(st_row)
        await verify.refresh(jg_row)
        assert st_row.corpus_quarantined is False
        assert jg_row.corpus_quarantined is False


@pytest.mark.asyncio
async def test_dry_run_does_not_update_rows(db):
    db.add(Statute(name="Dry Run Act", jurisdiction="Federal", source_name="Test"))
    await db.commit()
    before = (await db.execute(select(Statute).where(Statute.name == "Dry Run Act"))).scalars().first()
    assert before is not None
    assert before.corpus_quarantined is False

    out = await run_quarantine_bad_documents(apply=False)
    assert out["dry_run"] is True
    assert out["statute_bad_rows"] >= 1

    after = (await db.execute(select(Statute).where(Statute.name == "Dry Run Act"))).scalars().first()
    assert after is not None
    assert after.corpus_quarantined is False
