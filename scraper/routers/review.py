"""
/admin/review — the review queue; /admin/check — the check viewer (raw source beside the
deterministic, AI and reconciled extractions, with conflicts and the extraction audit trail).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import get_db
from scraper.models import ExtractionAudit, Judgment, QuarantineQueue, ScraperStaging, SourceProvenance, StatutesStaging, Treatment
from scraper.routers.auth import require_admin
from scraper.security import scrub_secrets
from scraper.tasks.promotion import resolve_quarantine

router = APIRouter(prefix="/admin", tags=["review"], dependencies=[Depends(require_admin)])


class Resolution(BaseModel):
    resolution: str  # promoted | rejected
    reviewer: str
    notes: Optional[str] = None
    corrected: Optional[Dict[str, Any]] = None


@router.get("/review")
async def review_queue(reviewed: bool = False, kind: Optional[str] = None, limit: int = Query(default=100, le=1000), db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    q = select(QuarantineQueue).where(QuarantineQueue.reviewed.is_(reviewed))
    if kind:
        q = q.where(QuarantineQueue.kind == kind)
    rows = (await db.execute(q.order_by(QuarantineQueue.created_at.desc()).limit(limit))).scalars().all()
    return [
        {
            "id": str(r.id),
            "kind": r.kind,
            "source": r.source_name,
            "source_url": r.source_url,
            "citation": r.extracted_citation,
            "title": r.extracted_title,
            "reason": r.reason,
            "confidence": r.confidence_score,
            "details": r.details,
            "staging_id": str(r.staging_id) if r.staging_id else None,
            "statutes_staging_id": str(r.statutes_staging_id) if r.statutes_staging_id else None,
            "treatment_candidate": r.treatment_candidate,
            "created_at": r.created_at.isoformat(),
            "reviewed": r.reviewed,
            "resolution": r.resolution,
        }
        for r in rows
    ]


@router.post("/review/{item_id}/resolve")
async def resolve(item_id: str, body: Resolution, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    item = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.id == item_id))).scalars().first()
    if item is None:
        raise HTTPException(404, "not found")
    if item.reviewed:
        raise HTTPException(409, "already reviewed")
    if body.resolution not in ("promoted", "rejected"):
        raise HTTPException(422, "resolution must be promoted|rejected")
    if item.kind == "treatment" and body.resolution == "promoted" and item.treatment_candidate:
        c = item.treatment_candidate
        db.add(Treatment(citing_judgment_id=c["citing_judgment_id"], cited_citation=c["cited_citation"], label=c["label"], confidence=1.0, evidence_passage=c["evidence_passage"][:400], method=c.get("method", "deterministic"), reviewed=True))
        item.reviewed, item.reviewed_by, item.resolution, item.resolution_notes = True, body.reviewer, "promoted", body.notes
        await db.commit()
        return {"result": "promoted"}
    result = await resolve_quarantine(db, item, reviewer=body.reviewer, resolution=body.resolution, notes=body.notes, corrected=body.corrected)
    await db.commit()
    return {"result": result}


@router.get("/check/{staging_id}")
async def check_viewer(staging_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == staging_id))).scalars().first()
    kind = "judgment"
    if st is None:
        st = (await db.execute(select(StatutesStaging).where(StatutesStaging.id == staging_id))).scalars().first()
        kind = "statute"
    if st is None:
        raise HTTPException(404, "staging row not found")
    prov = (await db.execute(select(SourceProvenance).where(SourceProvenance.id == st.provenance_id))).scalars().first()
    audits = (await db.execute(select(ExtractionAudit).where(ExtractionAudit.staging_id == st.id).order_by(ExtractionAudit.created_at.desc()))).scalars().all()
    return {
        "kind": kind,
        "staging_id": str(st.id),
        "source": st.source_name,
        "access_method": st.access_method,
        "source_url": st.source_url,
        "status": st.status,
        "confidence": st.confidence_score,
        "quarantine_reason": st.quarantine_reason,
        "validation_errors": st.validation_errors,
        "extraction_engine": st.extraction_engine,
        "raw_text": scrub_secrets((st.raw_text or "")[:200_000]),
        "raw_text_hash": getattr(st, "raw_text_hash", None),
        "deterministic": st.deterministic_json,
        "ai": st.ai_json,
        "reconciled": st.reconciled_json,
        "provenance": {"id": str(prov.id), "content_hash": prov.content_hash, "content_kind": prov.content_kind, "raw_ref": prov.raw_ref, "routes": prov.routes, "fetched_at": prov.fetched_at.isoformat(), "document_kind": prov.document_kind, "is_original_document": prov.is_original_document} if prov else None,
        "audit": [
            {"id": str(a.id), "extractor": a.extractor, "ai_mode": a.ai_mode, "status": a.status, "schema_version": a.schema_version, "conflicts": a.conflicts_json, "validation_errors": a.validation_errors_json, "credits": a.credits_or_cost, "elapsed_ms": a.elapsed_ms, "created_at": a.created_at.isoformat()}
            for a in audits
        ],
        "promoted_to_id": str(st.promoted_to_id) if st.promoted_to_id else None,
    }


@router.get("/check/judgment/{judgment_id}")
async def check_judgment(judgment_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    j = (await db.execute(select(Judgment).where(Judgment.id == judgment_id))).scalars().first()
    if j is None:
        raise HTTPException(404, "judgment not found")
    treatments = (await db.execute(select(Treatment).where(Treatment.citing_judgment_id == j.id))).scalars().all()
    return {
        "id": str(j.id),
        "canonical_citation": j.canonical_citation,
        "case_title": j.case_title,
        "court": j.court_name,
        "judges": j.judge_names,
        "bench_size": j.bench_size,
        "bench_type": j.bench_type,
        "decision_date": j.decision_date.isoformat() if j.decision_date else None,
        "year": j.year,
        "access_method": j.access_method,
        "has_original_pdf": j.has_original_pdf,
        "full_text_hash": j.full_text_hash,
        "full_text_excerpt": (j.full_text or "")[:5000],
        "statutes_cited": j.statutes_cited,
        "citations_cited": j.citations_cited,
        "treatments": [{"cited": t.cited_citation, "label": t.label, "confidence": t.confidence, "method": t.method, "evidence": t.evidence_passage} for t in treatments],
        "extraction_engine": j.extraction_engine,
        "confidence": j.confidence_score,
    }
