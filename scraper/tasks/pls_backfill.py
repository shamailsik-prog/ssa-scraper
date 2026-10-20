"""Idempotent PakistanLawSite backfills: case metadata for existing judgments, and re-evaluation of quarantined
captures with the corrected extractor. Both only ever ADD information; neither deletes rows or touches raw text."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celery import shared_task

from scraper.database import SessionLocal, run_async
from scraper.extractors import deterministic as det
from scraper.extractors.hybrid_extractor import load_court_directory
from scraper.extractors.validation import reconcile_judgment
from scraper.models import Citation, Judgment, QuarantineQueue, ScraperStaging
from scraper.tasks.promotion import normalize_docket_key
from scraper.parsers.case_metadata import parse_case_metadata

try:
    from scraper.parsers.order_sheet_title import case_title_from_order_sheet
except ImportError:  # order-sheet titles ship on a sibling layer PR
    case_title_from_order_sheet = None

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
                        if col == "docket_number":
                            j.docket_key = normalize_docket_key(value) or None
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


async def requalify_quarantined(db: AsyncSession, *, limit: Optional[int] = 2000, dry_run: bool = False, skip_final: bool = False) -> Dict[str, Any]:
    """Re-run the (deterministic, no-AI) extraction + validation on quarantined PLS captures whose citation is still not
    a judgment. Rows that now validate are re-promoted; rows that still fail stay quarantined with their new reason
    (bad pages are re-fetched by the grid walk instead, see quarantined_capture_retryable)."""
    from scraper.tasks.promotion import promote_judgment_staging

    from scraper.config import settings
    from scraper.models import ScraperSource

    directory = await load_court_directory(db)
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    min_confidence = float((source.extraction_min_confidence if source else None) or settings.SGAI_DEFAULT_MIN_CONFIDENCE)
    counts: Dict[str, Any] = {"scanned": 0, "promoted": 0, "duplicate": 0, "still_quarantined": 0, "still_by_reason": {}, "skipped_final": 0}
    held = (ScraperStaging.source_name == SOURCE_NAME, ScraperStaging.status == "quarantined", ScraperStaging.promoted_to_id.is_(None))
    q = select(ScraperStaging.id).where(*held)
    if skip_final:
        # the scheduled run leaves a judgment held on the merits (a citation conflict, a human decision) to a person;
        # filtered in the query, so merits holds never fill the batch ahead of rows that can be released
        from scraper.tasks.pakistanlawsite import _FINAL_QUARANTINE

        merits = func.coalesce(ScraperStaging.quarantine_reason, "").op("~*")(_FINAL_QUARANTINE.pattern.replace("(?i)", ""))
        q = q.where(~merits)
        counts["skipped_final"] = int((await db.execute(select(func.count()).select_from(ScraperStaging).where(*held, merits))).scalar() or 0)
    q = q.order_by(ScraperStaging.created_at)
    if limit is not None:
        q = q.limit(limit)
    ids = (await db.execute(q)).scalars().all()
    # One row at a time, re-selected after each commit/rollback: a rollback expires every loaded instance,
    # and touching an expired attribute under AsyncSession raises instead of lazy-loading.
    for st_id in ids:
        st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == st_id))).scalars().first()
        if st is None or st.status != "quarantined" or st.promoted_to_id is not None:
            continue  # released meanwhile (by promotion, a reviewer or another run)
        counts["scanned"] += 1
        text = st.raw_text or ""
        meta = {"citation": st.extracted_citation, "title": st.extracted_title, "court": st.extracted_court, "url": st.source_url}
        dj = det.extract_judgment_deterministic(html=None, text=text, source_meta=meta)
        out = reconcile_judgment(
            deterministic=dj,
            ai=None,
            raw_text=text,
            court_directory=directory,
            min_confidence=min_confidence,
            source_name=st.source_name or SOURCE_NAME,
        )
        if out.quarantine:
            counts["still_quarantined"] += 1
            key = (out.quarantine_reason or "")[:40]
            counts["still_by_reason"][key] = counts["still_by_reason"].get(key, 0) + 1
            if not dry_run:
                # keep the ledger truthful: the reason the row is set aside NOW, not the one from before the fixes,
                # in the staging row and in its open review item alike
                st.quarantine_reason = out.quarantine_reason
                for item in (
                    await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st_id, QuarantineQueue.reviewed.is_(False)))
                ).scalars().all():
                    item.reason = (out.quarantine_reason or item.reason)[:1000]
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
        found = reconciled.get("citations") or []
        # keep the grid row's own citation when the page carries it; a headnote-cited case must not replace it
        if not (st.extracted_citation and st.extracted_citation in found):
            st.extracted_citation = (found or [st.extracted_citation])[0]
        st.status = "extracted"
        st.quarantine_reason = None
        await db.flush()
        try:
            result = await promote_judgment_staging(db, st)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("requalify: promotion failed for %s", st_id)
            counts["still_quarantined"] += 1
            continue
        if result in ("promoted", "duplicate"):
            counts[result] += 1
            # the judgment no longer needs a person: close its open review item
            items = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st_id, QuarantineQueue.reviewed.is_(False)))).scalars().all()
            for item in items:
                item.reviewed = True
                item.reviewed_by = "system"
                item.reviewed_at = datetime.now(timezone.utc)
                item.resolution = "requalified"
                item.resolution_notes = f"re-checked with the corrected extractor: {result}"
            if items:
                await db.commit()
        else:
            counts["still_quarantined"] += 1
    return counts


async def backfill_order_sheet_titles(
    db: AsyncSession,
    *,
    batch: int = 500,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Rewrite case_title from the order-sheet header when SSA_ORDER_SHEET_TITLE logic applies.

    Only updates rows where the derived title differs from the stored title. Safe to dry-run."""
    if case_title_from_order_sheet is None:
        return {"skipped": "order_sheet_title parser not present on this branch"}
    counts = {"scanned": 0, "updated": 0, "cleared_bad": 0}
    last_id = None
    while True:
        q = select(Judgment).where(Judgment.source_name == SOURCE_NAME).order_by(Judgment.id).limit(batch)
        if last_id is not None:
            q = q.where(Judgment.id > last_id)
        rows = (await db.execute(q)).scalars().all()
        if not rows:
            break
        for j in rows:
            last_id = j.id
            counts["scanned"] += 1
            new_title = case_title_from_order_sheet(j.full_text or "")
            if not new_title or new_title == (j.case_title or ""):
                continue
            if not dry_run:
                j.case_title = new_title
            counts["updated"] += 1
            if limit and counts["scanned"] >= limit:
                break
        if not dry_run:
            await db.commit()
        if limit and counts["scanned"] >= limit:
            break
    return counts


async def run_backfill_metadata(**kw) -> Dict[str, int]:
    async with SessionLocal() as db:
        return await backfill_case_metadata(db, **kw)


async def run_backfill_order_sheet_titles(**kw) -> Dict[str, int]:
    async with SessionLocal() as db:
        return await backfill_order_sheet_titles(db, **kw)


async def backfill_pld_citations(
    db: AsyncSession,
    *,
    batch: int = 500,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    from scraper.parsers.pld_citation_relabel import relabel_pld_citation

    counts = {"scanned": 0, "updated": 0}
    last_id = None
    while True:
        q = (
            select(Judgment, Citation)
            .join(Citation, Citation.judgment_id == Judgment.id)
            .where(Judgment.source_name == SOURCE_NAME, Citation.is_primary.is_(True))
            .order_by(Judgment.id)
            .limit(batch)
        )
        if last_id is not None:
            q = q.where(Judgment.id > last_id)
        rows = (await db.execute(q)).all()
        if not rows:
            break
        for j, cit in rows:
            last_id = j.id
            counts["scanned"] += 1
            new = relabel_pld_citation(cit.citation_string or j.canonical_citation, j.full_text)
            if not new or new == j.canonical_citation:
                continue
            if not dry_run:
                j.canonical_citation = new
                cit.citation_string = new
            counts["updated"] += 1
            if limit and counts["scanned"] >= limit:
                break
        if not dry_run:
            await db.commit()
        if limit and counts["scanned"] >= limit:
            break
    return counts


async def run_backfill_pld_citations(**kw) -> Dict[str, int]:
    async with SessionLocal() as db:
        return await backfill_pld_citations(db, **kw)


REQUALIFY_LOCK_KEY = 0x55A_9702  # pg advisory lock: one requalify run at a time (scheduled or by hand)


async def run_requalify(**kw) -> Dict[str, Any]:
    from sqlalchemy import text as sql_text

    from scraper.database import engine

    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        if not (await conn.execute(sql_text("SELECT pg_try_advisory_lock(:k)"), {"k": REQUALIFY_LOCK_KEY})).scalar():
            return {"skipped": "requalify_run_in_progress"}
        try:
            async with SessionLocal() as db:
                return await requalify_quarantined(db, **kw)
        finally:
            await conn.execute(sql_text("SELECT pg_advisory_unlock(:k)"), {"k": REQUALIFY_LOCK_KEY})


@shared_task(name="scraper.tasks.pls_backfill.requalify_quarantined")
def requalify_quarantined_task(limit: Optional[int] = None, skip_final: bool = True):
    """Scheduled form of `pls_admin requalify-quarantine`: deterministic, stored text only, merits holds left alone."""
    return run_async(run_requalify(limit=limit, skip_final=skip_final))
