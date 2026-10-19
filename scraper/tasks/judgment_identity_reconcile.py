"""
Non-destructive reported ↔ unreported identity reconciliation (B2).

Links rows only on exact identity (court + normalised docket + decision date). Never merges text,
never deletes rows, and never uses text-similarity scoring.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import Judgment, JudgmentIdentityLink, JudgmentIdentityReview
from scraper.tasks.promotion import judgment_exact_identity_matches, normalize_docket_key

logger = logging.getLogger(__name__)

IDENTITY_REVIEW_HASH_MISMATCH = "hash_match_without_exact_identity"
IDENTITY_REVIEW_SAME_IDENTITY_DIFFERENT_TEXT = "exact_identity_different_full_text_hash"
IDENTITY_REVIEW_AMBIGUOUS_REPORTED = "multiple_reported_same_exact_identity"


def identity_tuple(
    judgment: Judgment,
) -> Optional[Tuple[UUID, str, Any]]:
    if judgment.court_id is None or judgment.decision_date is None:
        return None
    docket_key = normalize_docket_key(judgment.docket_number)
    if not docket_key:
        return None
    return (judgment.court_id, docket_key, judgment.decision_date)


async def link_exact_identity(
    db: AsyncSession,
    unreported_id: UUID,
    reported_id: UUID,
    *,
    source: str = "reconcile",
) -> JudgmentIdentityLink:
    unrep = (await db.execute(select(Judgment).where(Judgment.id == unreported_id))).scalars().one()
    rep = (await db.execute(select(Judgment).where(Judgment.id == reported_id))).scalars().one()
    if not unrep.unreported or rep.unreported:
        raise ValueError("link requires an unreported and a reported judgment")
    if not judgment_exact_identity_matches(
        unrep,
        court_id=rep.court_id,
        docket_number=rep.docket_number,
        decision_date=rep.decision_date,
    ):
        raise ValueError("exact identity does not match")
    if not unrep.full_text_hash or unrep.full_text_hash != rep.full_text_hash:
        raise ValueError("full text hash must match for reported/unreported link")
    exists = (
        await db.execute(
            select(JudgmentIdentityLink).where(
                JudgmentIdentityLink.unreported_judgment_id == unreported_id,
                JudgmentIdentityLink.reported_judgment_id == reported_id,
            )
        )
    ).scalars().first()
    if exists is not None:
        return exists
    row = JudgmentIdentityLink(
        unreported_judgment_id=unreported_id,
        reported_judgment_id=reported_id,
        source=source,
    )
    db.add(row)
    await db.flush()
    return row


async def flag_identity_review(
    db: AsyncSession,
    *,
    unreported_id: UUID,
    reported_id: Optional[UUID],
    reason: str,
    details: Optional[Dict[str, Any]] = None,
) -> JudgmentIdentityReview:
    open_row = (
        await db.execute(
            select(JudgmentIdentityReview).where(
                JudgmentIdentityReview.unreported_judgment_id == unreported_id,
                JudgmentIdentityReview.reported_judgment_id == reported_id,
                JudgmentIdentityReview.reason == reason,
                JudgmentIdentityReview.reviewed.is_(False),
            )
        )
    ).scalars().first()
    if open_row is not None:
        return open_row
    row = JudgmentIdentityReview(
        unreported_judgment_id=unreported_id,
        reported_judgment_id=reported_id,
        reason=reason[:200],
        details=details or {},
    )
    db.add(row)
    await db.flush()
    return row


async def reconcile_judgment_identity_pairs(db: AsyncSession, *, limit: Optional[int] = None) -> Dict[str, int]:
    """Scan unreported judgments and link or flag reported counterparts. Row count is never reduced."""
    counts = {"scanned": 0, "linked": 0, "flagged": 0}
    q = select(Judgment).where(Judgment.unreported.is_(True)).order_by(Judgment.created_at.desc())
    if limit is not None:
        q = q.limit(limit)
    unreported_rows = (await db.execute(q)).scalars().all()
    reported_rows = (
        await db.execute(select(Judgment).where(Judgment.unreported.is_(False)))
    ).scalars().all()
    by_identity: Dict[Tuple[UUID, str, Any], List[Judgment]] = {}
    for r in reported_rows:
        key = identity_tuple(r)
        if key is None:
            continue
        by_identity.setdefault(key, []).append(r)
    for u in unreported_rows:
        counts["scanned"] += 1
        u_key = identity_tuple(u)
        if u_key is None:
            continue
        identity_peers = by_identity.get(u_key, [])
        matches = [
            r
            for r in identity_peers
            if u.full_text_hash and r.full_text_hash and u.full_text_hash == r.full_text_hash
        ]
        if len(matches) == 1:
            await link_exact_identity(db, u.id, matches[0].id, source="reconcile")
            counts["linked"] += 1
            continue
        if len(matches) > 1:
            await flag_identity_review(
                db,
                unreported_id=u.id,
                reported_id=None,
                reason=IDENTITY_REVIEW_AMBIGUOUS_REPORTED,
                details={"reported_ids": [str(m.id) for m in matches]},
            )
            counts["flagged"] += 1
            continue
        for r in identity_peers:
            if u.full_text_hash and r.full_text_hash and u.full_text_hash != r.full_text_hash:
                await flag_identity_review(
                    db,
                    unreported_id=u.id,
                    reported_id=r.id,
                    reason=IDENTITY_REVIEW_SAME_IDENTITY_DIFFERENT_TEXT,
                    details={"unreported_id": str(u.id), "reported_id": str(r.id)},
                )
                counts["flagged"] += 1
        for r in reported_rows:
            if not u.full_text_hash or u.full_text_hash != r.full_text_hash:
                continue
            if judgment_exact_identity_matches(
                u,
                court_id=r.court_id,
                docket_number=r.docket_number,
                decision_date=r.decision_date,
            ):
                continue
            await flag_identity_review(
                db,
                unreported_id=u.id,
                reported_id=r.id,
                reason=IDENTITY_REVIEW_HASH_MISMATCH,
                details={"unreported_id": str(u.id), "reported_id": str(r.id)},
            )
            counts["flagged"] += 1
    await db.flush()
    return counts
