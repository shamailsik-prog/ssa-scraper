"""
/admin/corpus — browse judgments, citations, statutes with body completeness.

Firm LLM keys (SGAI_*/OPENAI_API_KEY) are optional enrichment only; they do NOT block
deterministic harvest. backfill_progress.complete=false means frontier_remaining > 0
(and/or optional count targets unmet) — not that scraping is broken.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import get_db
from scraper.harvest_mode import backfill_progress, get_harvest_mode, selected_source_names
from scraper.models import (
    Citation,
    CrawlCoverage,
    CrawlFrontier,
    Judgment,
    ScraperStaging,
    Statute,
    StatuteSection,
    StatuteSectionVersion,
    StatutesStaging,
)
from scraper.routers.auth import require_admin

router = APIRouter(prefix="/admin/corpus", tags=["corpus"], dependencies=[Depends(require_admin)])


def _has_body(text_val: Optional[str]) -> bool:
    return bool(text_val and text_val.strip())


@router.get("/summary")
async def corpus_summary(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Totals, staging extracted vs promoted, body completeness, frontier meaning."""
    judgments = int((await db.execute(select(func.count()).select_from(Judgment))).scalar() or 0)
    citations = int((await db.execute(select(func.count()).select_from(Citation))).scalar() or 0)
    statutes = int((await db.execute(select(func.count()).select_from(Statute))).scalar() or 0)
    sections = int((await db.execute(select(func.count()).select_from(StatuteSection))).scalar() or 0)
    section_versions = int((await db.execute(select(func.count()).select_from(StatuteSectionVersion))).scalar() or 0)

    j_empty = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Judgment)
                .where(or_(Judgment.full_text.is_(None), func.btrim(Judgment.full_text) == ""))
            )
        ).scalar()
        or 0
    )
    j_with = judgments - j_empty

    sec_with = int(
        (
            await db.execute(
                select(func.count())
                .select_from(StatuteSection)
                .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
                .where(func.length(func.btrim(StatuteSectionVersion.section_text)) > 0)
            )
        ).scalar()
        or 0
    )
    sec_empty = sections - sec_with

    staging_j = {
        str(status): int(n)
        for status, n in (
            await db.execute(select(ScraperStaging.status, func.count()).group_by(ScraperStaging.status))
        ).all()
    }
    staging_s = {
        str(status): int(n)
        for status, n in (
            await db.execute(select(StatutesStaging.status, func.count()).group_by(StatutesStaging.status))
        ).all()
    }

    frontier_by_source = [
        {"source": src, "status": status, "count": int(n)}
        for src, status, n in (
            await db.execute(
                select(CrawlFrontier.source_name, CrawlFrontier.status, func.count())
                .group_by(CrawlFrontier.source_name, CrawlFrontier.status)
                .order_by(CrawlFrontier.source_name, CrawlFrontier.status)
            )
        ).all()
    ]
    pls_remaining = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CrawlFrontier)
                .where(
                    CrawlFrontier.source_name == "PakistanLawSite",
                    CrawlFrontier.status.in_(["pending", "in_progress", "stale"]),
                )
            )
        ).scalar()
        or 0
    )
    pc_remaining = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CrawlFrontier)
                .where(
                    CrawlFrontier.source_name == "PakistanCode",
                    CrawlFrontier.status.in_(["pending", "in_progress", "stale"]),
                )
            )
        ).scalar()
        or 0
    )

    pls_volumes = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(CrawlCoverage.judgments_found > 0),
                func.count().filter(CrawlCoverage.volume_state == "closed"),
                func.coalesce(func.sum(CrawlCoverage.judgments_found), 0),
            ).where(CrawlCoverage.source_name == "PakistanLawSite")
        )
    ).one()
    volume_total = int(pls_volumes[0] or 0)
    volumes_with_hits = int(pls_volumes[1] or 0)
    volumes_closed = int(pls_volumes[2] or 0)
    judgments_found_cov = int(pls_volumes[3] or 0)

    sample_cursor = (
        await db.execute(
            select(CrawlCoverage)
            .where(CrawlCoverage.source_name == "PakistanLawSite", CrawlCoverage.volume_state == "open")
            .order_by(CrawlCoverage.updated_at.desc())
            .limit(5)
        )
    ).scalars().all()
    pls_cursors = [
        {
            "reporter": v.reporter,
            "year": v.year,
            "volume_state": v.volume_state,
            "highest_page_seen": v.highest_page_seen,
            "next_page_to_probe": v.next_page_to_probe,
            "judgments_found": v.judgments_found,
            "consecutive_misses": v.consecutive_misses,
        }
        for v in sample_cursor
    ]

    mode = await get_harvest_mode(db)
    selected = await selected_source_names(db, mode)
    progress = await backfill_progress(db, source_names=selected)

    statutes_with_sections = int(
        (await db.execute(select(func.count(func.distinct(StatuteSection.statute_id))))).scalar() or 0
    )
    pc_statutes = int(
        (await db.execute(select(func.count()).select_from(Statute).where(Statute.source_name == "PakistanCode"))).scalar()
        or 0
    )

    return {
        "totals": {
            "judgments": judgments,
            "citations": citations,
            "statutes": statutes,
            "statute_sections": sections,
            "statute_section_versions": section_versions,
            "statutes_with_at_least_one_section": statutes_with_sections,
        },
        "body_completeness": {
            "judgments_with_full_text": j_with,
            "judgments_empty_full_text": j_empty,
            "judgments_pct_empty": round(100.0 * j_empty / judgments, 2) if judgments else 0.0,
            "sections_with_text": sec_with,
            "sections_empty_or_missing_text": sec_empty,
            "sections_pct_empty": round(100.0 * sec_empty / sections, 2) if sections else 0.0,
        },
        "staging": {
            "scraper_staging_by_status": staging_j,
            "scraper_staging_extracted": staging_j.get("extracted", 0),
            "scraper_staging_promoted": staging_j.get("promoted", 0),
            "statutes_staging_by_status": staging_s,
            "statutes_staging_extracted": staging_s.get("extracted", 0),
            "statutes_staging_promoted": staging_s.get("promoted", 0),
        },
        "completeness": {
            "meaning": (
                "complete=false means the crawl frontier for selected backfill sources is not empty "
                "(frontier_remaining > 0) and/or optional BACKFILL_TARGET_* counts are unmet. "
                "It does NOT mean harvest is broken. Firm LLM keys (SGAI_*, OPENAI_API_KEY) are "
                "optional enrichment and do not block deterministic scrape."
            ),
            "backfill_progress": progress,
            "selected_sources": selected,
            "harvest_mode": mode,
            "pakistan_law_site": {
                "frontier_remaining": pls_remaining,
                "coverage_volumes_total": volume_total,
                "coverage_volumes_with_hits": volumes_with_hits,
                "coverage_volumes_closed": volumes_closed,
                "judgments_found_in_coverage": judgments_found_cov,
                "recent_open_cursors": pls_cursors,
            },
            "pakistan_code": {
                "frontier_remaining": pc_remaining,
                "statutes_promoted_from_source": pc_statutes,
            },
            "frontier_by_source_status": frontier_by_source,
        },
    }


@router.get("/judgments")
async def list_judgments(
    q: Optional[str] = Query(default=None, min_length=1),
    source: Optional[str] = None,
    empty_body: Optional[bool] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    filters = []
    if q:
        pat = f"%{q}%"
        filters.append(or_(Judgment.canonical_citation.ilike(pat), Judgment.case_title.ilike(pat)))
    if source:
        filters.append(Judgment.source_name == source)
    if empty_body is True:
        filters.append(or_(Judgment.full_text.is_(None), func.btrim(Judgment.full_text) == ""))
    elif empty_body is False:
        filters.append(Judgment.full_text.isnot(None))
        filters.append(func.length(func.btrim(Judgment.full_text)) > 0)

    count_q = select(func.count()).select_from(Judgment)
    list_q = select(Judgment)
    for f in filters:
        count_q = count_q.where(f)
        list_q = list_q.where(f)
    total = int((await db.execute(count_q)).scalar() or 0)
    rows = (await db.execute(list_q.order_by(Judgment.promoted_at.desc()).offset(offset).limit(limit))).scalars().all()
    items = [
        {
            "id": str(j.id),
            "citation": j.canonical_citation,
            "title": j.case_title,
            "court": j.court_name,
            "year": j.year,
            "source_name": j.source_name,
            "source_url": j.source_url,
            "access_method": j.access_method,
            "has_body": _has_body(j.full_text),
            "body_length": len(j.full_text) if j.full_text else 0,
            "promoted_at": j.promoted_at.isoformat() if j.promoted_at else None,
        }
        for j in rows
    ]
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.get("/judgments/{judgment_id}")
async def get_judgment(judgment_id: UUID, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    j = (await db.execute(select(Judgment).where(Judgment.id == judgment_id))).scalars().first()
    if j is None:
        raise HTTPException(404, "judgment not found")
    cites = (
        await db.execute(select(Citation).where(Citation.judgment_id == judgment_id).order_by(Citation.is_primary.desc()))
    ).scalars().all()
    return {
        "id": str(j.id),
        "citation": j.canonical_citation,
        "title": j.case_title,
        "court": j.court_name,
        "year": j.year,
        "decision_date": j.decision_date.isoformat() if j.decision_date else None,
        "judges": j.judge_names,
        "source_name": j.source_name,
        "source_url": j.source_url,
        "access_method": j.access_method,
        "has_original_pdf": j.has_original_pdf,
        "has_body": _has_body(j.full_text),
        "body_length": len(j.full_text) if j.full_text else 0,
        "full_text": j.full_text,
        "full_text_hash": j.full_text_hash,
        "citations": [
            {
                "id": str(c.id),
                "citation_string": c.citation_string,
                "reporter": c.reporter,
                "year": c.year,
                "is_primary": c.is_primary,
            }
            for c in cites
        ],
    }


@router.get("/citations")
async def list_citations(
    q: Optional[str] = Query(default=None, min_length=1),
    reporter: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    filters = []
    if q:
        pat = f"%{q}%"
        filters.append(
            or_(
                Citation.citation_string.ilike(pat),
                Judgment.case_title.ilike(pat),
                Judgment.canonical_citation.ilike(pat),
            )
        )
    if reporter:
        filters.append(Citation.reporter == reporter)

    base = select(Citation, Judgment).join(Judgment, Judgment.id == Citation.judgment_id)
    count_base = select(func.count()).select_from(Citation).join(Judgment, Judgment.id == Citation.judgment_id)
    for f in filters:
        base = base.where(f)
        count_base = count_base.where(f)
    total = int((await db.execute(count_base)).scalar() or 0)
    rows = (await db.execute(base.order_by(Citation.created_at.desc()).offset(offset).limit(limit))).all()
    items = [
        {
            "id": str(c.id),
            "citation_string": c.citation_string,
            "reporter": c.reporter,
            "year": c.year,
            "volume": c.volume,
            "page": c.page,
            "is_primary": c.is_primary,
            "judgment_id": str(j.id),
            "title": j.case_title,
            "source_name": j.source_name,
            "has_body": _has_body(j.full_text),
            "body_length": len(j.full_text) if j.full_text else 0,
        }
        for c, j in rows
    ]
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.get("/citations/{citation_id}")
async def get_citation(citation_id: UUID, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    row = (
        await db.execute(
            select(Citation, Judgment)
            .join(Judgment, Judgment.id == Citation.judgment_id)
            .where(Citation.id == citation_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "citation not found")
    c, j = row
    return {
        "id": str(c.id),
        "citation_string": c.citation_string,
        "raw_string": c.raw_string,
        "reporter": c.reporter,
        "year": c.year,
        "volume": c.volume,
        "page": c.page,
        "is_primary": c.is_primary,
        "source_evidence": c.source_evidence,
        "judgment": {
            "id": str(j.id),
            "citation": j.canonical_citation,
            "title": j.case_title,
            "court": j.court_name,
            "source_name": j.source_name,
            "source_url": j.source_url,
            "has_body": _has_body(j.full_text),
            "body_length": len(j.full_text) if j.full_text else 0,
            "full_text": j.full_text,
        },
    }


@router.get("/statutes")
async def list_statutes(
    q: Optional[str] = Query(default=None, min_length=1),
    source: Optional[str] = None,
    with_sections_only: bool = False,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    sec_stats = (
        select(
            StatuteSection.statute_id.label("statute_id"),
            func.count(StatuteSection.id).label("section_count"),
            func.count(StatuteSectionVersion.id)
            .filter(StatuteSectionVersion.section_text.isnot(None))
            .filter(func.length(func.btrim(StatuteSectionVersion.section_text)) > 0)
            .label("sections_with_text"),
        )
        .select_from(StatuteSection)
        .outerjoin(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
        .group_by(StatuteSection.statute_id)
        .subquery()
    )
    filters = []
    if q:
        pat = f"%{q}%"
        filters.append(or_(Statute.name.ilike(pat), Statute.short_name.ilike(pat)))
    if source:
        filters.append(Statute.source_name == source)
    if with_sections_only:
        filters.append(func.coalesce(sec_stats.c.section_count, 0) > 0)

    list_q = select(Statute, sec_stats.c.section_count, sec_stats.c.sections_with_text).outerjoin(
        sec_stats, sec_stats.c.statute_id == Statute.id
    )
    count_q = select(func.count()).select_from(Statute).outerjoin(sec_stats, sec_stats.c.statute_id == Statute.id)
    for f in filters:
        list_q = list_q.where(f)
        count_q = count_q.where(f)
    total = int((await db.execute(count_q)).scalar() or 0)
    rows = (await db.execute(list_q.order_by(Statute.created_at.desc()).offset(offset).limit(limit))).all()
    items = [
        {
            "id": str(st.id),
            "name": st.name,
            "short_name": st.short_name,
            "jurisdiction": st.jurisdiction,
            "year_enacted": st.year_enacted,
            "source_name": st.source_name,
            "source_url": st.source_url,
            "section_count": int(sc or 0),
            "sections_with_text": int(swt or 0),
            "has_body": int(swt or 0) > 0,
        }
        for st, sc, swt in rows
    ]
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.get("/statutes/{statute_id}")
async def get_statute(
    statute_id: UUID,
    include_section_text: bool = True,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    st = (await db.execute(select(Statute).where(Statute.id == statute_id))).scalars().first()
    if st is None:
        raise HTTPException(404, "statute not found")
    rows = (
        await db.execute(
            select(StatuteSection, StatuteSectionVersion)
            .outerjoin(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute_id)
            .order_by(StatuteSection.sort_key, StatuteSection.section_number)
        )
    ).all()
    sections: List[Dict[str, Any]] = []
    for sec, ver in rows:
        text_val = ver.section_text if ver else None
        entry: Dict[str, Any] = {
            "id": str(sec.id),
            "section_number": sec.section_number,
            "section_title": sec.section_title,
            "chapter": sec.chapter,
            "part": sec.part,
            "has_body": _has_body(text_val),
            "body_length": len(text_val) if text_val else 0,
            "version_no": ver.version_no if ver else None,
        }
        if include_section_text:
            entry["section_text"] = text_val
        sections.append(entry)
    return {
        "id": str(st.id),
        "name": st.name,
        "short_name": st.short_name,
        "jurisdiction": st.jurisdiction,
        "year_enacted": st.year_enacted,
        "statute_type": st.statute_type,
        "is_repealed": st.is_repealed,
        "source_name": st.source_name,
        "source_url": st.source_url,
        "section_count": len(sections),
        "sections_with_text": sum(1 for s in sections if s["has_body"]),
        "sections": sections,
    }
