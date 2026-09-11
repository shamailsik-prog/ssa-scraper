"""/admin/coverage — crawl_frontier and crawl_coverage are the truth for progress (Amendment §10)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import get_db
from scraper.models import CrawlCoverage, CrawlFrontier, Judgment, SearchFormMap
from scraper.routers.auth import require_admin

router = APIRouter(prefix="/admin/coverage", tags=["coverage"], dependencies=[Depends(require_admin)])


@router.get("")
async def coverage(source: Optional[str] = None, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    q = select(CrawlFrontier.source_name, CrawlFrontier.tier, CrawlFrontier.status, func.count(), func.coalesce(func.sum(CrawlFrontier.yield_count), 0)).group_by(CrawlFrontier.source_name, CrawlFrontier.tier, CrawlFrontier.status)
    if source:
        q = q.where(CrawlFrontier.source_name == source)
    tiers: Dict[str, Dict[str, Any]] = {}
    for src, tier, status, n, yld in (await db.execute(q)).all():
        t = tiers.setdefault(src, {}).setdefault(f"tier{tier}", {"pending": 0, "in_progress": 0, "done": 0, "retired": 0, "stale": 0, "yield": 0})
        t[status] = t.get(status, 0) + int(n)
        t["yield"] += int(yld)
    vq = select(CrawlCoverage)
    if source:
        vq = vq.where(CrawlCoverage.source_name == source)
    volumes = [
        {"source": v.source_name, "reporter": v.reporter, "year": v.year, "state": v.volume_state, "highest_page_seen": v.highest_page_seen, "next_page_to_probe": v.next_page_to_probe, "consecutive_misses": v.consecutive_misses, "judgments_found": v.judgments_found, "closed_at": v.closed_at.isoformat() if v.closed_at else None}
        for v in (await db.execute(vq.order_by(CrawlCoverage.reporter, CrawlCoverage.year.desc()))).scalars().all()
    ]
    per_source = (await db.execute(select(Judgment.source_name, func.count()).group_by(Judgment.source_name))).all()
    maps = [
        {"source": m.source_name, "version": m.map_version, "stale": m.stale, "consecutive_parse_failures": m.consecutive_parse_failures, "mapped_by": m.mapped_by, "mapped_at": m.mapped_at.isoformat(), "dom_hash": m.dom_hash[:12], "fields": [k for k in (m.fields or {}) if not k.startswith("_")]}
        for m in (await db.execute(select(SearchFormMap).where(SearchFormMap.is_active.is_(True)))).scalars().all()
    ]
    return {
        "volume_end_gap": settings.VOLUME_END_GAP,
        "subscribed_reporters": settings.subscribed_reporters or "NOT CONFIGURED",
        "earliest_year": settings.PLS_EARLIEST_YEAR or "NOT CONFIGURED",
        "tiers": tiers,
        "volumes": volumes,
        "judgments_per_source": {s: int(n) for s, n in per_source},
        "search_maps": maps,
    }


@router.get("/frontier")
async def frontier(source: Optional[str] = None, tier: Optional[int] = None, status: Optional[str] = None, limit: int = Query(default=100, le=1000), db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    q = select(CrawlFrontier)
    if source:
        q = q.where(CrawlFrontier.source_name == source)
    if tier is not None:
        q = q.where(CrawlFrontier.tier == tier)
    if status:
        q = q.where(CrawlFrontier.status == status)
    rows = (await db.execute(q.order_by(CrawlFrontier.tier, CrawlFrontier.priority, CrawlFrontier.updated_at.desc()).limit(limit))).scalars().all()
    return [
        {"id": str(r.id), "source": r.source_name, "tier": r.tier, "query_key": r.query_key, "cursor": r.cursor_json, "status": r.status, "priority": r.priority, "yield": r.yield_count, "attempts": r.attempts, "slot": r.slot_number, "last_error": r.last_error, "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None}
        for r in rows
    ]
