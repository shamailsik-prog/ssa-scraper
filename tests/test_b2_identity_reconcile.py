"""B2: non-destructive reported/unreported identity linking (exact identity + same text hash)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from scraper.fetchers import canonical_text_hash
from scraper.models import Court, Judgment, JudgmentIdentityLink, JudgmentIdentityReview
from scraper.tasks.judgment_identity_reconcile import (
    IDENTITY_REVIEW_HASH_MISMATCH,
    IDENTITY_REVIEW_SAME_IDENTITY_DIFFERENT_TEXT,
    flag_identity_review,
    link_exact_identity,
    reconcile_judgment_identity_pairs,
)
from tests.test_unreported_promotion import BODY, _held, _html
from scraper.tasks.promotion import promote_judgment_staging, release_unreported


@pytest.mark.asyncio
async def test_b2_exact_identity_and_hash_create_link(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "a" * 600
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED SC C.P. 1 of 2024 (2024-01-01)",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        docket_key="c.p.1of2024",
        decision_date=date(2024, 1, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR 1",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        docket_key="c.p.1of2024",
        decision_date=date(2024, 1, 1),
        unreported=False,
        reporter="SCMR",
    )
    db.add_all([unrep, rep])
    await db.commit()
    link = await link_exact_identity(db, unrep.id, rep.id, source="test")
    assert link.unreported_judgment_id == unrep.id and link.reported_judgment_id == rep.id


@pytest.mark.asyncio
async def test_b2_same_hash_different_docket_flags_not_links(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "b" * 600
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED SC A",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR 2",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 2 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    with pytest.raises(ValueError, match="exact identity"):
        await link_exact_identity(db, unrep.id, rep.id)
    await flag_identity_review(
        db,
        unreported_id=unrep.id,
        reported_id=rep.id,
        reason=IDENTITY_REVIEW_HASH_MISMATCH,
    )
    await db.commit()
    row = (
        await db.execute(
            select(JudgmentIdentityReview).where(
                JudgmentIdentityReview.unreported_judgment_id == unrep.id,
                JudgmentIdentityReview.reason == IDENTITY_REVIEW_HASH_MISMATCH,
            )
        )
    ).scalars().one()
    assert row.reported_judgment_id == rep.id


@pytest.mark.asyncio
async def test_b2_same_identity_different_text_flags_both_rows_kept(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    unrep = Judgment(
        canonical_citation="UNREPORTED SC C",
        full_text="c" * 600,
        full_text_hash=canonical_text_hash("c" * 600),
        court_id=sc.id,
        docket_number="C.P. 9 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR 9",
        full_text="d" * 600,
        full_text_hash=canonical_text_hash("d" * 600),
        court_id=sc.id,
        docket_number="C.P. 9 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    counts = await reconcile_judgment_identity_pairs(db)
    await db.commit()
    assert counts["linked"] == 0 and counts["flagged"] >= 1
    assert (
        await db.execute(
            select(JudgmentIdentityReview).where(
                JudgmentIdentityReview.reason == IDENTITY_REVIEW_SAME_IDENTITY_DIFFERENT_TEXT,
                JudgmentIdentityReview.unreported_judgment_id == unrep.id,
            )
        )
    ).scalars().first() is not None
    assert len((await db.execute(select(Judgment))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_b2_promotion_keeps_unreported_and_links_reported(db, source):
    st = await _held(db, source)
    await release_unreported(db)
    assert await promote_judgment_staging(db, st) == "promoted"
    unrep = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().one()
    old_identity = unrep.canonical_citation

    again = await _held(db, source)
    again.status, again.quarantine_reason = "extracted", None
    again.reconciled_json = {**(again.reconciled_json or {}), "citations": ["2024 SCMR 77"]}
    assert await promote_judgment_staging(db, again) == "promoted"
    reported = (await db.execute(select(Judgment).where(Judgment.id == again.promoted_to_id))).scalars().one()
    await db.refresh(unrep)

    assert unrep.unreported is True and unrep.canonical_citation == old_identity
    assert reported.unreported is False and reported.canonical_citation == "2024 SCMR 77"
    link = (
        await db.execute(
            select(JudgmentIdentityLink).where(
                JudgmentIdentityLink.unreported_judgment_id == unrep.id,
                JudgmentIdentityLink.reported_judgment_id == reported.id,
            )
        )
    ).scalars().one()
    assert link.source == "promotion"


def test_b2_no_similarity_merge_in_promotion():
    import inspect

    from scraper.tasks import promotion as promotion_mod

    src = inspect.getsource(promotion_mod.promote_judgment_staging)
    assert "0.92" not in src
    assert "SequenceMatcher" not in src


@pytest.mark.asyncio
async def test_mutant_b2_m1_link_requires_matching_hash(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body_u, body_r = "e" * 600, "f" * 600
    unrep = Judgment(
        canonical_citation="UNREPORTED SC M1",
        full_text=body_u,
        full_text_hash=canonical_text_hash(body_u),
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR M1",
        full_text=body_r,
        full_text_hash=canonical_text_hash(body_r),
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    with pytest.raises(ValueError, match="full text hash"):
        await link_exact_identity(db, unrep.id, rep.id)


@pytest.mark.asyncio
async def test_mutant_b2_m2_reconcile_skips_link_without_hash_match(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    unrep = Judgment(
        canonical_citation="UNREPORTED SC M2",
        full_text="g" * 600,
        full_text_hash=canonical_text_hash("g" * 600),
        court_id=sc.id,
        docket_number="C.P. 2 of 2024",
        decision_date=date(2024, 2, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR M2",
        full_text="h" * 600,
        full_text_hash=canonical_text_hash("h" * 600),
        court_id=sc.id,
        docket_number="C.P. 2 of 2024",
        decision_date=date(2024, 2, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    counts = await reconcile_judgment_identity_pairs(db)
    assert counts["linked"] == 0
