"""Mark promoted PakistanLawSite chrome-shell captures as grid-refetch eligible (dry-run by default)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.extractors.judgment_guards import (
    CHROME_STUB_REFETCH_ROUTE_KEY,
    detect_pls_case_description_chrome_stub,
    guard_reason,
)
from scraper.models import Judgment, ScraperStaging

SOURCE_NAME = "PakistanLawSite"


async def find_chrome_stub_promoted(
    db: AsyncSession,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    fixes: List[Dict[str, str]] = []
    scanned = 0
    stmt = (
        select(Judgment.id, Judgment.canonical_citation, Judgment.full_text)
        .where(Judgment.source_name == SOURCE_NAME, Judgment.full_text.is_not(None))
        .order_by(Judgment.promoted_at.desc().nullslast(), Judgment.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(int(limit))
    for jid, citation, full_text in (await db.execute(stmt)).all():
        scanned += 1
        signal = detect_pls_case_description_chrome_stub(full_text)
        if signal is None:
            continue
        fixes.append(
            {
                "judgment_id": str(jid),
                "canonical_citation": citation or "",
                "reason": guard_reason(signal),
            }
        )
    return {"scanned": scanned, "count": len(fixes), "fixes": fixes}


async def _latest_staging_for_judgment(db: AsyncSession, judgment_id: UUID) -> Optional[ScraperStaging]:
    return (
        await db.execute(
            select(ScraperStaging)
            .where(
                ScraperStaging.source_name == SOURCE_NAME,
                ScraperStaging.promoted_to_id == judgment_id,
            )
            .order_by(ScraperStaging.updated_at.desc(), ScraperStaging.id.desc())
            .limit(1)
        )
    ).scalars().first()


async def mark_chrome_stub_refetch_eligible(
    db: AsyncSession,
    fixes: List[Dict[str, str]],
    *,
    apply: bool = False,
) -> Dict[str, Any]:
    if not bool(getattr(settings, "SSA_PLS_CHROME_STUB_REFETCH_MARK", True)):
        return {"skipped": "SSA_PLS_CHROME_STUB_REFETCH_MARK is off", "dry_run": not apply}
    now = datetime.now(timezone.utc).isoformat()
    would_apply = 0
    applied = 0
    for row in fixes:
        jid = UUID(row["judgment_id"])
        staging = await _latest_staging_for_judgment(db, jid)
        if staging is None:
            continue
        route = dict(staging.route_json or {})
        if route.get(CHROME_STUB_REFETCH_ROUTE_KEY):
            continue
        would_apply += 1
        if not apply:
            continue
        route[CHROME_STUB_REFETCH_ROUTE_KEY] = True
        route["chrome_stub_refetch_marked_at"] = now
        staging.route_json = route
        staging.status = "quarantined"
        staging.quarantine_reason = row.get("reason") or "login_stub: case_description_chrome_shell"
        applied += 1
    return {
        "dry_run": not apply,
        "would_mark": would_apply,
        "marked": applied,
    }


async def run_chrome_stub_refetch_mark(*, apply: bool = False, limit: Optional[int] = None) -> Dict[str, Any]:
    from scraper.database import SessionLocal

    async with SessionLocal() as db:
        report = await find_chrome_stub_promoted(db, limit=limit)
        mark = await mark_chrome_stub_refetch_eligible(db, report["fixes"], apply=apply)
        if apply:
            await db.commit()
        return {**report, **mark}
