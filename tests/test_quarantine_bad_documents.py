"""B7: quarantine_bad_documents CLI criteria, apply/undo snapshots on document_quarantine."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scraper.models import CorpusMetadata, DocumentQuarantine, Judgment, Statute
from scraper.tasks.quarantine_bad_documents import (
    LATEST_RUN_KEY,
    JUDGMENT_QUARANTINE_REASON,
    STATUTE_QUARANTINE_REASON,
    _table_snapshot,
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
    assert "prod_reference_baselines" not in counts


@pytest.mark.asyncio
async def test_quarantine_apply_undo_restores_exact_prior_state(db):
    st = Statute(name="Quarantine Me Act", jurisdiction="Federal", source_name="Test")
    jg = Judgment(
        canonical_citation="2020 TEST 1",
        access_method="public",
        full_text="x",
        source_name="Test",
    )
    prior_st = Statute(name="Prior Reason Act", jurisdiction="Federal", source_name="Test")
    db.add_all([st, jg, prior_st])
    await db.flush()
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=prior_st.id,
            reason="operator:manual_review",
            run_id="prior-operator-run",
        )
    )
    await db.commit()

    from scraper.database import SessionLocal

    async with SessionLocal() as session:
        session.add(CorpusMetadata(key=LATEST_RUN_KEY, value="prior-latest-run"))
        await session.commit()
        before = await _table_snapshot(session)
        applied = await apply_quarantine(session)
        run_id = applied["run_id"]
        assert applied["statutes_updated"] >= 1
        assert applied["judgments_updated"] >= 1

        rows = (await session.execute(select(DocumentQuarantine))).scalars().all()
        by_key = {(r.doc_type, str(r.doc_id)): r for r in rows}
        assert by_key[("statute", str(st.id))].reason == STATUTE_QUARANTINE_REASON
        assert by_key[("statute", str(st.id))].run_id == run_id
        assert by_key[("judgment", str(jg.id))].reason == JUDGMENT_QUARANTINE_REASON
        assert by_key[("statute", str(prior_st.id))].reason == "operator:manual_review"
        assert by_key[("statute", str(prior_st.id))].run_id == "prior-operator-run"

        undone = await undo_quarantine(session, run_id)
        assert undone["rows_removed"] == applied["statutes_updated"] + applied["judgments_updated"]

        after = await _table_snapshot(session)
        assert after == before
        latest = (await session.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
        assert latest is not None
        assert latest.value == "prior-latest-run"


@pytest.mark.asyncio
async def test_dry_run_does_not_update_rows(db):
    db.add(Statute(name="Dry Run Act", jurisdiction="Federal", source_name="Test"))
    await db.commit()
    before_count = len((await db.execute(select(DocumentQuarantine))).scalars().all())

    out = await run_quarantine_bad_documents(apply=False)
    assert out["dry_run"] is True
    assert out["statute_bad_rows"] >= 1

    after_count = len((await db.execute(select(DocumentQuarantine))).scalars().all())
    assert after_count == before_count
