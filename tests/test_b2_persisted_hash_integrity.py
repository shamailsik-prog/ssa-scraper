"""B2 regression: stored text digests cannot authorize false reported/unreported links."""
from datetime import date

import pytest
from sqlalchemy import select

from scraper.fetchers import canonical_text_hash
from scraper.models import Court, Judgment, JudgmentIdentityLink, JudgmentIdentityReview
from scraper.tasks.judgment_identity_reconcile import (
    IDENTITY_REVIEW_HASH_MISMATCH,
    link_exact_identity,
    reconcile_judgment_identity_pairs,
)


async def _pair(db, unreported_text, reported_text, stored_hash):
    court = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    common = dict(
        court_id=court.id,
        docket_number="C.P. 4242 of 2024",
        decision_date=date(2024, 4, 2),
        full_text_hash=stored_hash,
    )
    unreported = Judgment(
        canonical_citation="UNREPORTED SC B2 persisted hash guard",
        full_text=unreported_text,
        unreported=True,
        **common,
    )
    reported = Judgment(
        canonical_citation="2024 SCMR 4242",
        full_text=reported_text,
        unreported=False,
        **common,
    )
    db.add_all([unreported, reported])
    await db.commit()
    return unreported, reported


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["different_bodies", "missing_unreported", "missing_reported", "stale_both"],
)
async def test_link_refuses_unverified_stored_digest(db, scenario):
    body = "Actual judgment body. " * 40
    digest = canonical_text_hash(body)
    left, right, stored = body, body, digest
    if scenario == "different_bodies":
        right = "Different judgment body. " * 40
    elif scenario == "missing_unreported":
        left = None
    elif scenario == "missing_reported":
        right = None
    elif scenario == "stale_both":
        stored = "0" * 64
    unreported, reported = await _pair(db, left, right, stored)
    with pytest.raises(ValueError, match="full text hash"):
        await link_exact_identity(db, unreported.id, reported.id)
    assert (await db.execute(select(JudgmentIdentityLink))).scalars().first() is None
    assert len((await db.execute(select(Judgment))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_reconcile_flags_stale_equal_hash_without_link_or_deletion(db):
    body = "Reported judgment body. " * 40
    stored = canonical_text_hash(body)
    unreported, reported = await _pair(db, body, "Other judgment body. " * 40, stored)
    counts = await reconcile_judgment_identity_pairs(db)
    await db.commit()
    assert counts["linked"] == 0 and counts["flagged"] >= 1
    assert (await db.execute(select(JudgmentIdentityReview).where(
        JudgmentIdentityReview.unreported_judgment_id == unreported.id,
        JudgmentIdentityReview.reported_judgment_id == reported.id,
        JudgmentIdentityReview.reason == IDENTITY_REVIEW_HASH_MISMATCH,
    ))).scalars().one() is not None
    assert (await db.execute(select(JudgmentIdentityLink))).scalars().first() is None
    assert len((await db.execute(select(Judgment))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_link_accepts_verified_whitespace_normalized_text_and_is_idempotent(db):
    left = "Judgment body with whitespace. " * 35
    right = "Judgment\n body with whitespace. " + "Judgment body with whitespace. " * 34
    digest = canonical_text_hash(left)
    assert canonical_text_hash(right) == digest
    unreported, reported = await _pair(db, left, right, digest)
    first, created = await link_exact_identity(db, unreported.id, reported.id)
    assert created is True
    second, created_again = await link_exact_identity(db, unreported.id, reported.id)
    assert created_again is False and second.id == first.id
