"""Read endpoints over the contract tables (kept from the original build; admin key required)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import get_db
from scraper.models import Judgment, Statute, StatuteSection, StatuteSectionVersion
from scraper.routers.auth import require_admin

router = APIRouter(prefix="/api", tags=["read"], dependencies=[Depends(require_admin)])


@router.get("/search")
async def search(q: str = Query(..., min_length=2), limit: int = Query(default=10, le=100), db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    pat = f"%{q}%"
    rows = (await db.execute(select(Judgment).where(or_(Judgment.canonical_citation.ilike(pat), Judgment.case_title.ilike(pat))).limit(limit))).scalars().all()
    return [{"id": str(j.id), "citation": j.canonical_citation, "title": j.case_title, "court": j.court_name, "year": j.year, "access_method": j.access_method} for j in rows]


@router.get("/judgments/{judgment_id}")
async def judgment(judgment_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    j = (await db.execute(select(Judgment).where(Judgment.id == judgment_id))).scalars().first()
    if j is None:
        raise HTTPException(404, "not found")
    return {"id": str(j.id), "citation": j.canonical_citation, "title": j.case_title, "court": j.court_name, "year": j.year, "decision_date": j.decision_date.isoformat() if j.decision_date else None, "judges": j.judge_names, "bench_size": j.bench_size, "bench_type": j.bench_type, "access_method": j.access_method, "has_original_pdf": j.has_original_pdf}


@router.get("/statutes")
async def statutes(statute_name: Optional[str] = None, limit: int = Query(default=20, le=200), db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    q = select(Statute).limit(limit)
    if statute_name:
        q = q.where(Statute.name.ilike(f"%{statute_name}%"))
    return [{"id": str(s.id), "name": s.name, "short_name": s.short_name, "jurisdiction": s.jurisdiction, "year_enacted": s.year_enacted} for s in (await db.execute(q)).scalars().all()]


@router.get("/statutes/{statute_id}/sections")
async def sections(statute_id: str, db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    rows = (await db.execute(select(StatuteSection, StatuteSectionVersion).outerjoin(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id).where(StatuteSection.statute_id == statute_id).order_by(StatuteSection.sort_key))).all()
    return [{"section_number": s.section_number, "title": s.section_title, "version_no": v.version_no if v else None, "version_confidence": v.version_confidence if v else None, "effective_from": v.effective_from.isoformat() if v and v.effective_from else None, "text": (v.section_text if v else None)} for s, v in rows]
