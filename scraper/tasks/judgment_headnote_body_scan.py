"""Scan promoted PakistanLawSite judgments for long headnote-only bodies (report / apply / undo)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.extractors.judgment_guards import detect_headnote_only_no_body, guard_reason
from scraper.models import DocumentQuarantine, Judgment

SOURCE_NAME = "PakistanLawSite"
HEADNOTE_BODY_SCAN_REASON_PREFIX = "headnote_only"
_SCAN_PAGE_SIZE = 250


async def find_headnote_only_promoted(
    db: AsyncSession,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    stmt = (
        select(Judgment.id, Judgment.canonical_citation, Judgment.full_text)
        .where(Judgment.source_name == SOURCE_NAME)
        .order_by(Judgment.promoted_at.desc().nullslast(), Judgment.id.desc())
        .execution_options(yield_per=_SCAN_PAGE_SIZE)
    )
    fixes: List[Dict[str, str]] = []
    scanned = 0
    stream = await db.stream(stmt)
    try:
        async for row in stream:
            if limit is not None and scanned >= int(limit):
                break
            jid, citation, full_text = row
            scanned += 1
            signal = detect_headnote_only_no_body(full_text)
            if signal is None:
                continue
            fixes.append(
                {
                    "id": str(jid),
                    "canonical_citation": citation or "",
                    "reason": guard_reason(signal),
                    "signal": signal.signal,
                }
            )
    finally:
        await stream.close()
    return {"scanned": scanned, "count": len(fixes), "fixes": fixes}


async def apply_headnote_body_quarantine(db: AsyncSession, run_id: str, fixes: List[Dict[str, str]]) -> int:
    now = datetime.now(timezone.utc)
    applied: List[UUID] = []
    for row in fixes:
        jid = UUID(row["id"])
        # A scan is only a proposal; revalidate current source and text under lock.
        judgment = (
            await db.execute(
                select(Judgment)
                .where(Judgment.id == jid, Judgment.source_name == SOURCE_NAME)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if judgment is None:
            continue
        signal = detect_headnote_only_no_body(judgment.full_text)
        if signal is None:
            continue
        exists = (
            await db.execute(
                select(DocumentQuarantine.doc_id).where(
                    DocumentQuarantine.doc_type == "judgment",
                    DocumentQuarantine.doc_id == jid,
                )
            )
        ).first()
        if exists:
            continue
        db.add(
            DocumentQuarantine(
                doc_type="judgment",
                doc_id=jid,
                run_id=run_id,
                reason=guard_reason(signal)[:500],
                quarantined_at=now,
            )
        )
        applied.append(jid)
    return len(applied)


async def _delete_quarantine_by_run_id(db: AsyncSession, run_id: str, *, doc_ids: Optional[List[UUID]] = None) -> int:
    stmt = delete(DocumentQuarantine).where(
        DocumentQuarantine.doc_type == "judgment",
        DocumentQuarantine.run_id == run_id,
        DocumentQuarantine.reason.like(f"{HEADNOTE_BODY_SCAN_REASON_PREFIX}:%"),
    )
    if doc_ids:
        stmt = stmt.where(DocumentQuarantine.doc_id.in_(doc_ids))
    result = await db.execute(stmt)
    return int(result.rowcount or 0)


async def undo_headnote_body_scan(db: AsyncSession, run_id: str) -> int:
    """Undo only headnote-only quarantine rows from this run.

    Durable database run IDs survive Redis expiry; unrelated quarantine reasons
    are deliberately retained even when a run ID is reused.
    """
    return await _delete_quarantine_by_run_id(db, run_id)


async def run_headnote_body_scan(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
) -> Dict[str, Any]:
    from scraper.database import SessionLocal

    if undo:
        async with SessionLocal() as db:
            removed = await undo_headnote_body_scan(db, undo)
            await db.commit()
            return {"undo": undo, "removed": removed}
    if apply and not bool(getattr(settings, "SSA_PLS_HEADNOTE_BODY_GUARD", False)) and not force:
        return {
            "error": "SSA_PLS_HEADNOTE_BODY_GUARD is off; enable the flag or pass --force to apply",
            "dry_run": True,
        }
    async with SessionLocal() as db:
        report = await find_headnote_only_promoted(db, limit=limit)
        if not apply:
            report["dry_run"] = True
            return report
        run_id = secrets.token_hex(6)
        applied = await apply_headnote_body_quarantine(db, run_id, report["fixes"])
        await db.commit()
        return {**report, "dry_run": False, "run_id": run_id, "applied": applied}
