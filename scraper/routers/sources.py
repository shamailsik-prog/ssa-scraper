"""
/admin/sources — source configuration and status (Amendment §7, §18; Annex B-8).

No credential card: the login source shows its access method, its two slot states and the
human-login control instead. Secrets are never echoed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import EXTRACTION_MODES, settings
from scraper.database import get_db
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.models import BrowserSessionSlot, ExtractionAudit, Judgment, ScraperSource, SgaiUsageDaily
from scraper.routers.auth import require_admin

router = APIRouter(prefix="/admin/sources", tags=["sources"], dependencies=[Depends(require_admin)])


class ExtractionSettings(BaseModel):
    ai_extract_enabled: Optional[bool] = None
    extraction_mode: Optional[str] = None
    extraction_min_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    scrapegraph_schema_version: Optional[int] = Field(default=None, ge=1)
    crawl_allowed: Optional[bool] = None
    crawl_max_depth: Optional[int] = Field(default=None, ge=0, le=5)
    crawl_max_pages: Optional[int] = Field(default=None, ge=1, le=5000)


class StateChange(BaseModel):
    action: str  # pause | resume | re_enable | disable | enable
    reason: Optional[str] = None
    reviewed_by: Optional[str] = None


class SourceConfig(BaseModel):
    listings: Optional[List[str]] = None
    statute_urls: Optional[List[str]] = None
    allow_list: Optional[List[str]] = None
    document_cdn_hosts: Optional[List[str]] = None
    scrape_frequency_hours: Optional[int] = Field(default=None, ge=1)
    is_active: Optional[bool] = None


async def _source(db: AsyncSession, name: str) -> ScraperSource:
    s = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == name))).scalars().first()
    if s is None:
        raise HTTPException(404, "source not found")
    return s


async def source_view(
    db: AsyncSession,
    s: ScraperSource,
    local_engine: Optional[LocalScrapeGraphEngine] = None,
) -> Dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    usage = (await db.execute(select(SgaiUsageDaily).where(SgaiUsageDaily.day == today, SgaiUsageDaily.source_name == s.source_name))).scalars().all()
    managed = next((u for u in usage if u.engine_mode == "managed"), None)
    local = next((u for u in usage if u.engine_mode == "local"), None)
    total_ai = sum(u.calls for u in usage)
    ok_ai = sum(u.successes for u in usage)
    conflicts = sum(u.conflicts for u in usage)
    cache_hits = sum(u.cache_hits for u in usage)
    last_err = (await db.execute(select(ExtractionAudit).where(ExtractionAudit.source_name == s.source_name, ExtractionAudit.status.not_in(["ok", "cache_hit"])).order_by(ExtractionAudit.created_at.desc()))).scalars().first()
    view: Dict[str, Any] = {
        "source_name": s.source_name,
        "display_name": s.display_name,
        "source_url": s.source_url,
        "access_method": s.access_method,
        "state": s.state,
        "state_reason": s.state_reason,
        "requires_admin_review": s.requires_admin_review,
        "is_active": s.is_active,
        "scrape_case_law": s.scrape_case_law,
        "scrape_statutes": s.scrape_statutes,
        "scrape_instruments": s.scrape_instruments,
        "scrape_frequency_hours": s.scrape_frequency_hours,
        "allow_list": s.allow_list,
        "last_scraped_at": s.last_scraped_at.isoformat() if s.last_scraped_at else None,
        "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
        "last_error": s.last_error,
        "next_scrape_at": s.next_scrape_at.isoformat() if s.next_scrape_at else None,
        "records_today": (await db.execute(select(func.count()).select_from(Judgment).where(Judgment.source_name == s.source_name, Judgment.promoted_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)))).scalar(),
        "total_pages_scraped": s.total_pages_scraped,
        "total_records_extracted": s.total_records_extracted,
        "extraction": {
            "mode": s.extraction_mode,
            "effective_engine": _effective_engine(s),
            "scrapegraph": "enabled" if (settings.SGAI_ENABLED and s.ai_extract_enabled and s.extraction_mode != "deterministic") else "disabled",
            "managed_calls_today": managed.calls if managed else 0,
            "managed_credits_today": managed.credits if managed else 0.0,
            "local_calls_today": local.calls if local else 0,
            "cache_hits_today": cache_hits,
            "ai_success_rate_today": (round(ok_ai / total_ai, 3) if total_ai else None),
            "ai_validation_conflicts_today": conflicts,
            "last_ai_error": s.last_ai_error or (f"{last_err.status}" if last_err else None),
            "schema_version": s.scrapegraph_schema_version,
            "min_confidence": s.extraction_min_confidence,
            "local_model_status": (local_engine or LocalScrapeGraphEngine()).status if s.access_method == "login_session" or s.extraction_mode == "scrapegraph_local" else None,
        },
    }
    if s.access_method == "login_session":
        slots = (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == s.source_name).order_by(BrowserSessionSlot.slot_number))).scalars().all()
        view["login_scraping_permitted"] = settings.login_scraping_effective
        view["slots"] = [
            {"slot": sl.slot_number, "role": sl.role, "state": sl.state, "reason": sl.state_reason, "logged_in_at": sl.logged_in_at.isoformat() if sl.logged_in_at else None, "last_used_at": sl.last_used_at.isoformat() if sl.last_used_at else None, "reconnects": sl.reconnect_count}
            for sl in slots
        ]
        view["current_slot"] = (s.config_json or {}).get("current_slot")
        view["pacing"] = (s.config_json or {}).get("pacing") or {}
        view["not_configured"] = [k for k in settings.not_configured() if k.startswith("PLS_")]
    return view


def _effective_engine(s: ScraperSource) -> str:
    if not settings.SGAI_ENABLED or not s.ai_extract_enabled or s.extraction_mode == "deterministic":
        return "deterministic"
    if s.access_method == "login_session":
        return "deterministic + local" if settings.sgai_local_configured else "deterministic (local NOT CONFIGURED)"
    if s.extraction_mode == "scrapegraph_local":
        return "local" if settings.sgai_local_configured else "deterministic (local NOT CONFIGURED)"
    if s.extraction_mode == "scrapegraph_managed":
        return "managed" if settings.sgai_managed_configured else "deterministic (managed NOT CONFIGURED)"
    if settings.sgai_managed_configured:
        return "hybrid (managed)"
    if settings.sgai_local_configured:
        return "hybrid (local)"
    return "deterministic (no engine configured)"


@router.get("")
async def list_sources(db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    engine = LocalScrapeGraphEngine()
    return [
        await source_view(db, s, engine)
        for s in (await db.execute(select(ScraperSource).order_by(ScraperSource.source_name))).scalars().all()
    ]


@router.get("/{name}/status")
async def status(name: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    return await source_view(db, await _source(db, name))


@router.post("/{name}/trigger")
async def trigger(name: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, name)
    if s.state in ("HALTED", "DISABLED"):
        raise HTTPException(409, f"source is {s.state}: {s.state_reason}")
    from scraper.tasks.celery_app import app

    if s.access_method == "login_session":
        if not settings.login_scraping_effective:
            raise HTTPException(409, "ALLOW_LOGIN_SCRAPING is false or ENVIRONMENT != chambers")
        app.send_task("scraper.tasks.dispatcher.run_login_session_job", args=(name,), queue="login_session")
    else:
        app.send_task("scraper.tasks.dispatcher.run_source_job", args=(name,), queue="scraper")
    return {"message": f"triggered {name}"}


@router.post("/{name}/extraction-settings")
async def extraction_settings(name: str, body: ExtractionSettings, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, name)
    if body.extraction_mode is not None:
        mode = body.extraction_mode.lower()
        if mode not in EXTRACTION_MODES:
            raise HTTPException(422, f"extraction_mode must be one of {EXTRACTION_MODES}")
        if s.access_method == "login_session" and mode == "scrapegraph_managed":
            raise HTTPException(422, "login_session sources may never use the managed ScrapeGraph API")
        s.extraction_mode = mode
    for k in ("ai_extract_enabled", "extraction_min_confidence", "scrapegraph_schema_version", "crawl_allowed", "crawl_max_depth", "crawl_max_pages"):
        v = getattr(body, k)
        if v is not None:
            if k == "crawl_allowed" and v and s.access_method != "public":
                raise HTTPException(422, "crawl is permitted for PUBLIC sources only")
            setattr(s, k, v)
    await db.commit()
    return await source_view(db, s)


@router.post("/{name}/state")
async def change_state(name: str, body: StateChange, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, name)
    now = datetime.now(timezone.utc)
    if body.action == "pause":
        s.state, s.state_reason = "PAUSED", body.reason or "paused by admin"
    elif body.action == "resume":
        if s.state == "HALTED":
            raise HTTPException(409, "a HALTED source needs re_enable with reviewed_by")
        s.state, s.state_reason = "ACTIVE", None
    elif body.action == "re_enable":
        if not body.reviewed_by:
            raise HTTPException(422, "re_enable requires reviewed_by (admin review of the block)")
        s.state, s.state_reason, s.requires_admin_review = "ACTIVE", f"re-enabled by {body.reviewed_by}: {body.reason or ''}", False
        s.is_active = True
        s.next_scrape_at = now
        for sl in (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == s.source_name, BrowserSessionSlot.state == "HALTED"))).scalars().all():
            sl.state, sl.state_reason = "NEEDS_HUMAN_LOGIN", "re-enabled after block review; fresh human login required"
    elif body.action == "disable":
        s.state, s.state_reason, s.is_active = "DISABLED", body.reason or "disabled by admin", False
    elif body.action == "enable":
        s.is_active = True
        s.state = "ACTIVE"
        s.state_reason = body.reason or "enabled by admin"
        s.requires_admin_review = False
        s.next_scrape_at = now
    else:
        raise HTTPException(422, "action must be pause|resume|re_enable|disable|enable")
    s.state_changed_at = now
    await db.commit()
    return await source_view(db, s)


@router.post("/{name}/config")
async def config(name: str, body: SourceConfig, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, name)
    cfg = dict(s.config_json or {})
    if body.listings is not None:
        cfg["listings"] = body.listings
    if body.statute_urls is not None:
        cfg["statute_urls"] = body.statute_urls
    s.config_json = cfg
    if body.allow_list is not None:
        s.allow_list = [h.strip().lower() for h in body.allow_list if h.strip()]
    if body.document_cdn_hosts is not None:
        s.document_cdn_hosts = [h.strip().lower() for h in body.document_cdn_hosts if h.strip()]
    if body.scrape_frequency_hours is not None:
        s.scrape_frequency_hours = body.scrape_frequency_hours
    if cfg:
        s.config_json = cfg
    if body.is_active is not None:
        s.is_active = body.is_active
    await db.commit()
    return await source_view(db, s)
