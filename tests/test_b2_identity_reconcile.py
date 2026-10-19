"""B2: non-destructive reported/unreported identity linking (exact identity + same text hash)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from scraper.fetchers import canonical_text_hash
from scraper.models import Court, Judgment, JudgmentIdentityLink, JudgmentIdentityReview
from scraper.tasks.judgment_identity_reconcile import (
    IDENTITY_REVIEW_AMBIGUOUS_REPORTED,
    IDENTITY_REVIEW_HASH_MISMATCH,
    IDENTITY_REVIEW_SAME_IDENTITY_DIFFERENT_TEXT,
    link_exact_identity,
    reconcile_judgment_identity_pairs,
)
from scraper.tasks.promotion import promote_judgment_staging, release_unreported
from tests.test_unreported_promotion import _held


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
    link, created = await link_exact_identity(db, unrep.id, rep.id, source="test")
    assert created is True
    assert link.unreported_judgment_id == unrep.id and link.reported_judgment_id == rep.id


@pytest.mark.asyncio
async def test_n12_scan_same_hash_different_docket_flags_with_counts(db):
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
    counts = await reconcile_judgment_identity_pairs(db)
    await db.commit()
    assert counts["linked"] == 0 and counts["flagged"] == 1
    row = (
        await db.execute(
            select(JudgmentIdentityReview).where(
                JudgmentIdentityReview.unreported_judgment_id == unrep.id,
                JudgmentIdentityReview.reason == IDENTITY_REVIEW_HASH_MISMATCH,
            )
        )
    ).scalars().one()
    assert row.reported_judgment_id == rep.id
    assert (
        await db.execute(select(JudgmentIdentityLink).where(JudgmentIdentityLink.unreported_judgment_id == unrep.id))
    ).scalars().first() is None


@pytest.mark.asyncio
async def test_n13_two_reported_same_identity_and_hash_flags_no_link(db, monkeypatch):
    from uuid import uuid4

    import scraper.tasks.judgment_identity_reconcile as mod

    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "n13" * 200
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED SC N13",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 10 of 2024",
        decision_date=date(2024, 10, 1),
        unreported=True,
    )
    db.add(unrep)
    await db.commit()
    await db.refresh(unrep)
    rep_a = mod._IdentityRow(
        id=uuid4(),
        court_id=sc.id,
        docket_number="C.P. 10 of 2024",
        decision_date=date(2024, 10, 1),
        full_text_hash=h,
        unreported=False,
    )
    rep_b = mod._IdentityRow(
        id=uuid4(),
        court_id=sc.id,
        docket_number="C.P. 10 of 2024",
        decision_date=date(2024, 10, 1),
        full_text_hash=h,
        unreported=False,
    )
    key = mod.identity_tuple_from_row(rep_a)

    async def _fake_load(_db):
        return {h: [rep_a, rep_b]}, {key: [rep_a, rep_b]}

    monkeypatch.setattr(mod, "_load_reported_identity_rows", _fake_load)
    counts = await reconcile_judgment_identity_pairs(db)
    await db.commit()
    assert counts["linked"] == 0 and counts["flagged"] == 1
    review = (
        await db.execute(
            select(JudgmentIdentityReview).where(
                JudgmentIdentityReview.unreported_judgment_id == unrep.id,
                JudgmentIdentityReview.reason == IDENTITY_REVIEW_AMBIGUOUS_REPORTED,
            )
        )
    ).scalars().one()
    assert review.reported_judgment_id is None
    assert (
        await db.execute(select(JudgmentIdentityLink).where(JudgmentIdentityLink.unreported_judgment_id == unrep.id))
    ).scalars().first() is None


@pytest.mark.asyncio
async def test_n1_scan_same_hash_different_court_flags_not_linked(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    lhc = (await db.execute(select(Court).where(Court.short_code == "LHC"))).scalars().one()
    body = "n1" * 200
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED SC N1",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR N1",
        full_text=body,
        full_text_hash=h,
        court_id=lhc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    counts = await reconcile_judgment_identity_pairs(db)
    assert counts["linked"] == 0 and counts["flagged"] == 1


@pytest.mark.asyncio
async def test_n2_scan_same_hash_different_date_flags_not_linked(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "n2" * 200
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED SC N2",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 1, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR N2",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 2, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    counts = await reconcile_judgment_identity_pairs(db)
    assert counts["linked"] == 0 and counts["flagged"] == 1


@pytest.mark.asyncio
async def test_n3_scan_dockets_differ_only_after_sixth_normalized_char_flags(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "n3" * 200
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED SC N3",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="Abcdef 1 of 2024",
        decision_date=date(2024, 3, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR N3",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="Abcdef 2 of 2024",
        decision_date=date(2024, 3, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    counts = await reconcile_judgment_identity_pairs(db)
    assert counts["linked"] == 0 and counts["flagged"] == 1
    with pytest.raises(ValueError):
        await link_exact_identity(db, unrep.id, rep.id)


@pytest.mark.asyncio
async def test_scan_never_raises_when_link_guard_rejects(db, monkeypatch):
    import scraper.tasks.judgment_identity_reconcile as mod

    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "guard" * 120
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED guard",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 4, 1),
        unreported=True,
    )
    rep_match = Judgment(
        canonical_citation="2024 SCMR guard-a",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 4, 1),
        unreported=False,
    )
    rep_conflict = Judgment(
        canonical_citation="2024 SCMR guard-b",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 9 of 2024",
        decision_date=date(2024, 4, 1),
        unreported=False,
    )
    db.add_all([unrep, rep_match, rep_conflict])
    await db.commit()

    async def _raise_link(*_a, **_k):
        raise ValueError("mutant link guard")

    monkeypatch.setattr(mod, "link_exact_identity", _raise_link)
    counts = await reconcile_judgment_identity_pairs(db)
    await db.commit()
    assert counts["linked"] == 0 and counts["flagged"] >= 2
    row = (
        await db.execute(
            select(JudgmentIdentityReview).where(
                JudgmentIdentityReview.unreported_judgment_id == unrep.id,
                JudgmentIdentityReview.reason == IDENTITY_REVIEW_HASH_MISMATCH,
                JudgmentIdentityReview.reported_judgment_id == rep_match.id,
            )
        )
    ).scalars().one()
    assert row is not None


@pytest.mark.asyncio
async def test_reconcile_links_exact_match_and_flags_conflicting_hash_peer(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "combo" * 120
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED combo",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 5, 1),
        unreported=True,
    )
    rep_link = Judgment(
        canonical_citation="2024 SCMR combo-a",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 5, 1),
        unreported=False,
    )
    rep_conflict = Judgment(
        canonical_citation="2024 SCMR combo-b",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 9 of 2024",
        decision_date=date(2024, 5, 1),
        unreported=False,
    )
    db.add_all([unrep, rep_link, rep_conflict])
    await db.commit()
    counts = await reconcile_judgment_identity_pairs(db)
    await db.commit()
    assert counts["linked"] == 1 and counts["flagged"] == 1
    assert (
        await db.execute(
            select(JudgmentIdentityLink).where(
                JudgmentIdentityLink.unreported_judgment_id == unrep.id,
                JudgmentIdentityLink.reported_judgment_id == rep_link.id,
            )
        )
    ).scalars().one()
    conflict_review = (
        await db.execute(
            select(JudgmentIdentityReview).where(
                JudgmentIdentityReview.unreported_judgment_id == unrep.id,
                JudgmentIdentityReview.reported_judgment_id == rep_conflict.id,
                JudgmentIdentityReview.reason == IDENTITY_REVIEW_HASH_MISMATCH,
            )
        )
    ).scalars().one()
    assert conflict_review is not None


@pytest.mark.asyncio
async def test_reconcile_linked_count_only_new_link_rows(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "lnk" * 120
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED lnk",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 7, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR lnk",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 7, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    first = await reconcile_judgment_identity_pairs(db)
    await db.commit()
    assert first["linked"] == 1
    second = await reconcile_judgment_identity_pairs(db)
    assert second["linked"] == 0


@pytest.mark.asyncio
async def test_reconcile_flagged_count_only_new_review_rows(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "cnt" * 120
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED cnt",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 6, 1),
        unreported=True,
    )
    rep = Judgment(
        canonical_citation="2024 SCMR cnt",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="C.P. 2 of 2024",
        decision_date=date(2024, 6, 1),
        unreported=False,
    )
    db.add_all([unrep, rep])
    await db.commit()
    first = await reconcile_judgment_identity_pairs(db)
    await db.commit()
    assert first["flagged"] == 1
    second = await reconcile_judgment_identity_pairs(db)
    assert second["flagged"] == 0


@pytest.mark.asyncio
async def test_n5_exactly_one_exact_identity_different_text_review(db):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    unrep = Judgment(
        canonical_citation="UNREPORTED SC N5",
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
    assert counts["linked"] == 0 and counts["flagged"] == 1
    rows = (
        await db.execute(
            select(JudgmentIdentityReview).where(
                JudgmentIdentityReview.reason == IDENTITY_REVIEW_SAME_IDENTITY_DIFFERENT_TEXT,
                JudgmentIdentityReview.unreported_judgment_id == unrep.id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert len((await db.execute(select(Judgment))).scalars().all()) == 2


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


def test_b2_reconcile_celery_task_registered():
    from scraper.tasks.celery_app import app

    assert "scraper.tasks.judgment_identity_reconcile.reconcile_judgment_identity_pairs" in app.tasks


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
    assert counts["linked"] == 0 and counts["flagged"] >= 1
