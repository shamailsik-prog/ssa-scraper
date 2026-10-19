"""
Non-destructive reported ↔ unreported identity reconciliation (B2).

Links rows only on exact identity (court + normalised docket + decision date). Never merges text,
never deletes rows, and never uses text-similarity scoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal, run_async
from scraper.models import Judgment, JudgmentIdentityLink, JudgmentIdentityReview
from scraper.tasks.promotion import judgment_exact_identity_matches, normalize_docket_key

logger = logging.getLogger(__name__)

IDENTITY_REVIEW_HASH_MISMATCH = "hash_match_without_exact_identity"
IDENTITY_REVIEW_SAME_IDENTITY_DIFFERENT_TEXT = "exact_identity_different_full_text_hash"
IDENTITY_REVIEW_AMBIGUOUS_REPORTED = "multiple_reported_same_exact_identity"

_IDENTITY_COLUMNS = (
    Judgment.id,
    Judgment.court_id,
    Judgment.docket_number,
    Judgment.decision_date,
    Judgment.full_text_hash,
)


@dataclass(frozen=True)
class _IdentityRow:
    id: UUID
    court_id: UUID
    docket_number: Optional[str]
    decision_date: Any
    full_text_hash: Optional[str]
    unreported: bool = False


def identity_tuple(
    judgment: Judgment,
) -> Optional[Tuple[UUID, str, Any]]:
    if judgment.court_id is None or judgment.decision_date is None:
        return None
    docket_key = normalize_docket_key(judgment.docket_number)
    if not docket_key:
        return None
    return (judgment.court_id, docket_key, judgment.decision_date)


def identity_tuple_from_row(row: _IdentityRow) -> Optional[Tuple[UUID, str, Any]]:
    if row.court_id is None or row.decision_date is None:
        return None
    docket_key = normalize_docket_key(row.docket_number)
    if not docket_key:
        return None
    return (row.court_id, docket_key, row.decision_date)


def _rows_exact_identity_match(left: _IdentityRow, right: _IdentityRow) -> bool:
    return judgment_exact_identity_matches(
        Judgment(
            court_id=left.court_id,
            docket_number=left.docket_number,
            decision_date=left.decision_date,
        ),
        court_id=right.court_id,
        docket_number=right.docket_number,
        decision_date=right.decision_date,
    )


def _row_from_result(row: Any, *, unreported: bool) -> _IdentityRow:
    return _IdentityRow(
        id=row.id,
        court_id=row.court_id,
        docket_number=row.docket_number,
        decision_date=row.decision_date,
        full_text_hash=row.full_text_hash,
        unreported=unreported,
    )


async def _load_reported_identity_rows(
    db: AsyncSession,
) -> Tuple[Dict[str, List[_IdentityRow]], Dict[Tuple[UUID, str, Any], List[_IdentityRow]]]:
    q = select(*_IDENTITY_COLUMNS).where(Judgment.unreported.is_(False))
    reported: List[_IdentityRow] = []
    for row in (await db.execute(q)).all():
        reported.append(_row_from_result(row, unreported=False))
    by_hash: Dict[str, List[_IdentityRow]] = {}
    by_identity: Dict[Tuple[UUID, str, Any], List[_IdentityRow]] = {}
    for r in reported:
        if r.full_text_hash:
            by_hash.setdefault(r.full_text_hash, []).append(r)
        key = identity_tuple_from_row(r)
        if key is not None:
            by_identity.setdefault(key, []).append(r)
    return by_hash, by_identity


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
    if False and (not unrep.full_text_hash or unrep.full_text_hash != rep.full_text_hash):
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
) -> Tuple[JudgmentIdentityReview, bool]:
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
        return open_row, False
    row = JudgmentIdentityReview(
        unreported_judgment_id=unreported_id,
        reported_judgment_id=reported_id,
        reason=reason[:200],
        details=details or {},
    )
    db.add(row)
    await db.flush()
    return row, True


async def reconcile_judgment_identity_pairs(db: AsyncSession, *, limit: Optional[int] = None) -> Dict[str, int]:
    """Scan unreported judgments and link or flag reported counterparts. Row count is never reduced."""
    counts = {"scanned": 0, "linked": 0, "flagged": 0}
    by_hash, by_identity = await _load_reported_identity_rows(db)

    q = (
        select(*_IDENTITY_COLUMNS)
        .where(Judgment.unreported.is_(True))
        .order_by(Judgment.created_at.desc())
    )
    if limit is not None:
        q = q.limit(limit)
    unreported_rows = [_row_from_result(row, unreported=True) for row in (await db.execute(q)).all()]

    for u in unreported_rows:
        counts["scanned"] += 1
        u_key = identity_tuple_from_row(u)
        if u_key is None:
            continue

        identity_peers = by_identity.get(u_key, [])
        hash_matches = [
            r
            for r in identity_peers
            if u.full_text_hash and r.full_text_hash and u.full_text_hash == r.full_text_hash
        ]

        linked_reported_id: Optional[UUID] = None

        if len(hash_matches) > 1:
            _, created = await flag_identity_review(
                db,
                unreported_id=u.id,
                reported_id=None,
                reason=IDENTITY_REVIEW_AMBIGUOUS_REPORTED,
                details={"reported_ids": [str(m.id) for m in hash_matches]},
            )
            if created:
                counts["flagged"] += 1
            continue

        if len(hash_matches) == 1:
            try:
                await link_exact_identity(db, u.id, hash_matches[0].id, source="reconcile")
                counts["linked"] += 1
                linked_reported_id = hash_matches[0].id
            except ValueError:
                _, created = await flag_identity_review(
                    db,
                    unreported_id=u.id,
                    reported_id=hash_matches[0].id,
                    reason=IDENTITY_REVIEW_HASH_MISMATCH,
                    details={"unreported_id": str(u.id), "reported_id": str(hash_matches[0].id)},
                )
                if created:
                    counts["flagged"] += 1

        for r in identity_peers:
            if linked_reported_id is not None and r.id == linked_reported_id:
                continue
            if u.full_text_hash and r.full_text_hash and u.full_text_hash != r.full_text_hash:
                _, created = await flag_identity_review(
                    db,
                    unreported_id=u.id,
                    reported_id=r.id,
                    reason=IDENTITY_REVIEW_SAME_IDENTITY_DIFFERENT_TEXT,
                    details={"unreported_id": str(u.id), "reported_id": str(r.id)},
                )
                if created:
                    counts["flagged"] += 1

        if not u.full_text_hash:
            continue
        for r in by_hash.get(u.full_text_hash, []):
            if linked_reported_id is not None and r.id == linked_reported_id:
                continue
            if _rows_exact_identity_match(u, r):
                continue
            _, created = await flag_identity_review(
                db,
                unreported_id=u.id,
                reported_id=r.id,
                reason=IDENTITY_REVIEW_HASH_MISMATCH,
                details={"unreported_id": str(u.id), "reported_id": str(r.id)},
            )
            if created:
                counts["flagged"] += 1

    await db.flush()
    return counts


async def _reconcile_judgment_identity_pairs_job(limit: Optional[int] = None) -> Dict[str, int]:
    async with SessionLocal() as db:
        counts = await reconcile_judgment_identity_pairs(db, limit=limit)
        await db.commit()
        return counts


@shared_task(name="scraper.tasks.judgment_identity_reconcile.reconcile_judgment_identity_pairs")
def reconcile_judgment_identity_pairs_task(limit: Optional[int] = None) -> Dict[str, int]:
    return run_async(_reconcile_judgment_identity_pairs_job(limit))
