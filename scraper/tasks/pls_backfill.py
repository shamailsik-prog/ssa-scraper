"""Idempotent PakistanLawSite backfills: case metadata for existing judgments, and re-evaluation of quarantined
captures with the corrected extractor. Both only ever ADD information; neither deletes rows or touches raw text."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal
from scraper.extractors import deterministic as det
from scraper.extractors.hybrid_extractor import load_court_directory
from scraper.extractors.validation import reconcile_judgment
from scraper.models import Judgment, ScraperStaging
from scraper.parsers.case_metadata import parse_case_metadata

logger = logging.getLogger(__name__)

SOURCE_NAME = "PakistanLawSite"


async def backfill_case_metadata(db: AsyncSession, *, batch: int = 500, limit: Optional[int] = None, dry_run: bool = False) -> Dict[str, int]:
    """Fill docket_number / petitioner / respondent / headnotes from the preserved full text, only where the column is
    still empty (never overwrites a value). Keyset-paged by id so it is safe to re-run and to interrupt."""
    counts = {"scanned": 0, "updated": 0, "docket_number": 0, "petitioner": 0, "respondent": 0, "headnotes": 0}
    last_id = None
    while True:
        q = (
            select(Judgment)
            .where(
                Judgment.source_name == SOURCE_NAME,
                or_(Judgment.docket_number.is_(None), Judgment.petitioner.is_(None), Judgment.respondent.is_(None), Judgment.headnotes.is_(None)),
            )
            .order_by(Judgment.id)
            .limit(batch)
        )
        if last_id is not None:
            q = q.where(Judgment.id > last_id)
        rows = (await db.execute(q)).scalars().all()
        if not rows:
            break
        for j in rows:
            last_id = j.id
            counts["scanned"] += 1
            meta = parse_case_metadata(j.full_text or "")
            changed = False
            for col in ("docket_number", "petitioner", "respondent", "headnotes"):
                value = getattr(meta, col)
                if value and not getattr(j, col):
                    if not dry_run:
                        setattr(j, col, value)
                    counts[col] += 1
                    changed = True
            counts["updated"] += int(changed)
            if limit and counts["scanned"] >= limit:
                break
        if not dry_run:
            await db.commit()
        if limit and counts["scanned"] >= limit:
            break
    return counts


async def requalify_quarantined(db: AsyncSession, *, limit: int = 2000, dry_run: bool = False) -> Dict[str, Any]:
    """Re-run the (deterministic, no-AI) extraction + validation on quarantined PLS captures whose citation is still not
    a judgment. Rows that now validate are re-promoted; rows that still fail stay quarantined with their new reason
    (bad pages are re-fetched by the grid walk instead, see quarantined_capture_retryable)."""
    from scraper.tasks.promotion import promote_judgment_staging

    directory = await load_court_directory(db)
    counts: Dict[str, Any] = {"scanned": 0, "promoted": 0, "duplicate": 0, "still_quarantined": 0, "still_by_reason": {}}
    rows = (
        await db.execute(
            select(ScraperStaging)
            .where(ScraperStaging.source_name == SOURCE_NAME, ScraperStaging.status == "quarantined", ScraperStaging.promoted_to_id.is_(None))
            .order_by(ScraperStaging.created_at)
            .limit(limit)
        )
    ).scalars().all()
    for st in rows:
        counts["scanned"] += 1
        text = st.raw_text or ""
        meta = {"citation": st.extracted_citation, "title": st.extracted_title, "court": st.extracted_court, "url": st.source_url}
        dj = det.extract_judgment_deterministic(html=None, text=text, source_meta=meta)
        out = reconcile_judgment(deterministic=dj, ai=None, raw_text=text, court_directory=directory, min_confidence=0.85)
        if out.quarantine:
            counts["still_quarantined"] += 1
            key = (out.quarantine_reason or "")[:40]
            counts["still_by_reason"][key] = counts["still_by_reason"].get(key, 0) + 1
            if not dry_run:
                # keep the ledger truthful: the reason the row is set aside NOW, not the one from before the fixes
                st.quarantine_reason = out.quarantine_reason
                st.validation_errors = out.errors
                st.confidence_score = out.confidence
                if out.data:
                    st.reconciled_json = {**(st.reconciled_json or {}), **dict(out.data)}
                await db.commit()
            continue
        if dry_run:
            counts["promoted"] += 1
            continue
        reconciled = dict(out.data or {})
        reconciled["document_type"] = (st.reconciled_json or {}).get("document_type") or reconciled.get("document_type")
        st.reconciled_json = reconciled
        st.confidence_score = out.confidence
        st.validation_errors = out.errors
        st.extracted_citation = (reconciled.get("citations") or [st.extracted_citation])[0]
        st.status = "extracted"
        st.quarantine_reason = None
        await db.flush()
        try:
            result = await promote_judgment_staging(db, st)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("requalify: promotion failed for %s", st.id)
            counts["still_quarantined"] += 1
            continue
        if result == "promoted":
            counts["promoted"] += 1
        elif result == "duplicate":
            counts["duplicate"] += 1
        else:
            counts["still_quarantined"] += 1
    return counts


async def run_backfill_metadata(**kw) -> Dict[str, int]:
    async with SessionLocal() as db:
        return await backfill_case_metadata(db, **kw)


async def run_requalify(**kw) -> Dict[str, Any]:
    async with SessionLocal() as db:
        return await requalify_quarantined(db, **kw)
