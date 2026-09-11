"""/admin/jobs, /admin/notifications, /admin/errors — operational status without secrets."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import get_db
from scraper.models import ExtractionAudit, Notification, QuarantineQueue, ScraperJob, ScraperStaging, StatutesStaging
from scraper.routers.auth import require_admin
from scraper.security import scrub_secrets

router = APIRouter(prefix="/admin", tags=["jobs"], dependencies=[Depends(require_admin)])


@router.get("/jobs")
async def jobs(limit: int = Query(default=50, le=500), db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    rows = (await db.execute(select(ScraperJob).order_by(ScraperJob.created_at.desc()).limit(limit))).scalars().all()
    return [{"id": str(j.id), "source": j.source_name, "type": j.job_type, "status": j.status, "started_at": j.started_at.isoformat() if j.started_at else None, "finished_at": j.finished_at.isoformat() if j.finished_at else None, "pages": j.pages_scraped, "extracted": j.records_extracted, "quarantined": j.records_quarantined, "error": scrub_secrets(j.error_message or "")[:500] or None, "summary": j.result_summary} for j in rows]


@router.get("/notifications")
async def notifications(acknowledged: bool = False, limit: int = Query(default=100, le=1000), db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    rows = (await db.execute(select(Notification).where(Notification.acknowledged.is_(acknowledged)).order_by(Notification.created_at.desc()).limit(limit))).scalars().all()
    return [{"id": str(n.id), "level": n.level, "code": n.code, "source": n.source_name, "message": n.message, "details": n.details, "created_at": n.created_at.isoformat()} for n in rows]


@router.post("/notifications/{nid}/ack")
async def ack(nid: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    n = (await db.execute(select(Notification).where(Notification.id == nid))).scalars().first()
    if n is None:
        raise HTTPException(404, "not found")
    n.acknowledged = True
    await db.commit()
    return {"acknowledged": True}


@router.get("/errors")
async def errors(limit: int = Query(default=50, le=500), db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    failed_jobs = (await db.execute(select(ScraperJob).where(ScraperJob.status == "failed").order_by(ScraperJob.created_at.desc()).limit(limit))).scalars().all()
    ai_failures = (await db.execute(select(ExtractionAudit).where(ExtractionAudit.status.not_in(["ok", "cache_hit"])).order_by(ExtractionAudit.created_at.desc()).limit(limit))).scalars().all()
    counts = {
        "staging_pending": (await db.execute(select(func.count()).select_from(ScraperStaging).where(ScraperStaging.status == "pending"))).scalar(),
        "staging_quarantined": (await db.execute(select(func.count()).select_from(ScraperStaging).where(ScraperStaging.status == "quarantined"))).scalar(),
        "statutes_staging_quarantined": (await db.execute(select(func.count()).select_from(StatutesStaging).where(StatutesStaging.status == "quarantined"))).scalar(),
        "review_queue_open": (await db.execute(select(func.count()).select_from(QuarantineQueue).where(QuarantineQueue.reviewed.is_(False)))).scalar(),
    }
    return {
        "counts": counts,
        "failed_jobs": [{"id": str(j.id), "source": j.source_name, "error": scrub_secrets(j.error_message or "")[:500], "at": j.created_at.isoformat()} for j in failed_jobs],
        "ai_failures": [{"id": str(a.id), "source": a.source_name, "extractor": a.extractor, "status": a.status, "at": a.created_at.isoformat()} for a in ai_failures],
    }
