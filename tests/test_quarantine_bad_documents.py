"""B7: quarantine_bad_documents CLI criteria, apply/undo snapshots on test DB."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scraper.models import Judgment, Statute
from scraper.tasks.quarantine_bad_documents import (
    JUDGMENT_QUARANTINE_REASON,
    STATUTE_QUARANTINE_REASON,
    apply_quarantine,
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
async def test_quarantine_apply_undo_restores_exact_prior_state(db):
    st = Statute(
        name="Quarantine Me Act",
        jurisdiction="Federal",
        source_name="Test",
        corpus_quarantined=False,
        corpus_quarantine_reason=None,
    )
    jg = Judgment(
        canonical_citation="2020 TEST 1",
        access_method="public",
        full_text="x",
        source_name="Test",
        corpus_quarantined=False,
        corpus_quarantine_reason=None,
    )
    prior_st = Statute(
        name="Prior Reason Act",
        jurisdiction="Federal",
        source_name="Test",
        corpus_quarantined=True,
        corpus_quarantine_reason="operator:manual_review",
    )
    db.add_all([st, jg, prior_st])
    await db.commit()

    from scraper.database import SessionLocal

    async with SessionLocal() as session:
        applied = await apply_quarantine(session)
        run_id = applied["run_id"]
        assert applied["statutes_updated"] >= 1
        assert applied["judgments_updated"] >= 1

        st_row = (await session.execute(select(Statute).where(Statute.name == "Quarantine Me Act"))).scalars().first()
        jg_row = (await session.execute(select(Judgment).where(Judgment.canonical_citation == "2020 TEST 1"))).scalars().first()
        prior_row = (await session.execute(select(Statute).where(Statute.name == "Prior Reason Act"))).scalars().first()
        assert st_row.corpus_quarantined is True
        assert st_row.corpus_quarantine_reason == STATUTE_QUARANTINE_REASON
        assert jg_row.corpus_quarantined is True
        assert jg_row.corpus_quarantine_reason == JUDGMENT_QUARANTINE_REASON
        assert prior_row.corpus_quarantine_reason == "operator:manual_review"

        undone = await undo_quarantine(session, run_id)
        assert undone["statutes_restored"] == applied["statutes_updated"]
        assert undone["judgments_restored"] == applied["judgments_updated"]

        await session.refresh(st_row)
        await session.refresh(jg_row)
        await session.refresh(prior_row)
        assert st_row.corpus_quarantined is False
        assert st_row.corpus_quarantine_reason is None
        assert jg_row.corpus_quarantined is False
        assert jg_row.corpus_quarantine_reason is None
        assert prior_row.corpus_quarantine_reason == "operator:manual_review"


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
