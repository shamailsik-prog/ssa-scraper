"""Scan promoted PakistanLawSite judgments for long headnote-only bodies (report / apply / undo)."""

from __future__ import annotations

import json
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
RUN_META_PREFIX = "judgment_headnote_body_scan:"


def _run_meta_key(run_id: str) -> str:
    return f"{RUN_META_PREFIX}{run_id}"


async def find_headnote_only_promoted(
    db: AsyncSession,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    stmt = (
        select(Judgment.id, Judgment.canonical_citation, Judgment.full_text)
        .where(Judgment.source_name == SOURCE_NAME)
        .order_by(Judgment.promoted_at.desc().nullslast())
    )
    if limit is not None:
        stmt = stmt.limit(int(limit))
    fixes: List[Dict[str, str]] = []
    scanned = 0
    for jid, citation, full_text in (await db.execute(stmt)).all():
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
    return {"scanned": scanned, "count": len(fixes), "fixes": fixes}


async def _record_run(db: AsyncSession, run_id: str, doc_ids: List[UUID]) -> None:
    import redis.asyncio as aioredis

    payload = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "doc_ids": [str(x) for x in doc_ids],
    }
    client = aioredis.from_url(settings.REDIS_URL)
    try:
        await client.set(_run_meta_key(run_id), json.dumps(payload), ex=86400 * 30)
    finally:
        await client.aclose()


async def apply_headnote_body_quarantine(db: AsyncSession, run_id: str, fixes: List[Dict[str, str]]) -> int:
    now = datetime.now(timezone.utc)
    applied: List[UUID] = []
    for row in fixes:
        jid = UUID(row["id"])
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
                reason=(row.get("reason") or "headnote_only: no_body_after_hearing_or_counsel")[:500],
                quarantined_at=now,
            )
        )
        applied.append(jid)
    await _record_run(db, run_id, applied)
    return len(applied)


async def undo_headnote_body_scan(db: AsyncSession, run_id: str) -> int:
    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.REDIS_URL)
    try:
        raw = await client.get(_run_meta_key(run_id))
    finally:
        await client.aclose()
    if not raw:
        return 0
    payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    doc_ids = [UUID(x) for x in payload.get("doc_ids") or []]
    if not doc_ids:
        return 0
    result = await db.execute(
        delete(DocumentQuarantine).where(
            DocumentQuarantine.doc_type == "judgment",
            DocumentQuarantine.run_id == run_id,
            DocumentQuarantine.doc_id.in_(doc_ids),
        )
    )
    return int(result.rowcount or 0)


async def run_headnote_body_scan(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    from scraper.database import SessionLocal

    if undo:
        async with SessionLocal() as db:
            removed = await undo_headnote_body_scan(db, undo)
            await db.commit()
            return {"undo": undo, "removed": removed}
    async with SessionLocal() as db:
        report = await find_headnote_only_promoted(db, limit=limit)
        if not apply:
            report["dry_run"] = True
            return report
        run_id = secrets.token_hex(6)
        applied = await apply_headnote_body_quarantine(db, run_id, report["fixes"])
        await db.commit()
        return {**report, "dry_run": False, "run_id": run_id, "applied": applied}
