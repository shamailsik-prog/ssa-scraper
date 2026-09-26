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
from scraper.harvest_mode import (
    backfill_progress,
    cadence_for_source,
    get_harvest_mode,
    set_harvest_mode,
    source_backfill_priority,
    source_selected_for_mode,
)
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.models import BrowserSessionSlot, CorpusMetadata, ExtractionAudit, Judgment, ScraperSource, SgaiUsageDaily
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
    action: str  # pause | resume | retry_now | re_enable | clear_block_retry | disable | enable
    reason: Optional[str] = None
    reviewed_by: Optional[str] = None


class SourceConfig(BaseModel):
    listings: Optional[List[str]] = None
    statute_urls: Optional[List[str]] = None
    allow_list: Optional[List[str]] = None
    document_cdn_hosts: Optional[List[str]] = None
    scrape_frequency_hours: Optional[int] = Field(default=None, ge=1)
    update_frequency_hours: Optional[int] = Field(default=None, ge=1, le=168)
    backfill_frequency_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    backfill_priority: Optional[int] = Field(default=None, ge=1, le=1000)
    backfill_enabled: Optional[bool] = None
    update_enabled: Optional[bool] = None
    auto_retry_on_block: Optional[bool] = None
    block_retry_cooldown_minutes: Optional[int] = Field(default=None, ge=1, le=2880)
    block_retry_max_attempts: Optional[int] = Field(default=None, ge=1, le=100)
    is_active: Optional[bool] = None


class HarvestModeBody(BaseModel):
    mode: str = Field(description="backfill | updates")
    changed_by: Optional[str] = None
    reason: Optional[str] = None


async def _source(db: AsyncSession, name: str) -> ScraperSource:
    s = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == name))).scalars().first()
    if s is None:
        raise HTTPException(404, "source not found")
    return s


def _to_int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _citation_grid_progress_view(source_name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    cursor_raw = cfg.get("citation_grid_cursor")
    cursor = dict(cursor_raw) if isinstance(cursor_raw, dict) else {}
    row_offset = _to_int_or_none(cursor.get("row_offset"))
    cursor_view = dict(cursor)
    cursor_view["row_offset"] = row_offset if row_offset is not None else 0
    status: Dict[str, Any] = {
        "source_name": source_name,
        "job_key": "PakistanLawSite:archivedpatientGrid",
        "citation_grid_cursor": cursor_view,
    }
    for shard in (0, 1):
        shard_key = f"citation_grid_cursor_shard_{shard}"
        shard_raw = cfg.get(shard_key)
        if isinstance(shard_raw, dict):
            shard_view = dict(shard_raw)
            shard_offset = _to_int_or_none(shard_raw.get("row_offset"))
            shard_view["row_offset"] = shard_offset if shard_offset is not None else 0
            status[shard_key] = shard_view
    last_flush: Dict[str, Any] = {}
    last_start_offset = _to_int_or_none(cursor.get("last_start_offset"))
    if last_start_offset is not None:
        last_flush["offset_before"] = last_start_offset
    if row_offset is not None:
        last_flush["offset_after"] = row_offset
    last_take_count = _to_int_or_none(cursor.get("last_take_count"))
    if last_take_count is not None:
        last_flush["processed_rows"] = last_take_count
    for field in ("offset_before", "offset_after", "staged_this_flush", "processed_rows"):
        if field in last_flush:
            continue
        parsed = _to_int_or_none(cursor.get(field))
        if parsed is not None:
            last_flush[field] = parsed
    if last_flush:
        status["last_flush"] = last_flush
    return status


async def source_view(
    db: AsyncSession,
    s: ScraperSource,
    local_engine: Optional[LocalScrapeGraphEngine] = None,
    mode: Optional[str] = None,
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
    cfg = dict(s.config_json or {})
    mode = mode or await get_harvest_mode(db)
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
        "scheduler": {
            "selected_for_mode": source_selected_for_mode(s, mode),
            "update_enabled": bool(cfg.get("update_enabled", True)),
            "update_frequency_hours": int(cfg.get("update_frequency_hours") or settings.UPDATE_CADENCE_HOURS),
            "backfill_enabled": bool(cfg.get("backfill_enabled", True)),
            "backfill_priority": int(cfg.get("backfill_priority", 100)),
            "backfill_frequency_minutes": int(cfg.get("backfill_frequency_minutes") or settings.BACKFILL_SOURCE_FREQUENCY_MINUTES),
            "effective_interval_minutes": cadence_for_source(s, mode),
            "auto_retry_on_block": bool(cfg.get("auto_retry_on_block", False)),
            "block_retry_cooldown_minutes": int(cfg.get("block_retry_cooldown_minutes") or settings.BLOCK_RETRY_COOLDOWN_MINUTES),
            "block_retry_max_attempts": int(cfg.get("block_retry_max_attempts") or settings.BLOCK_RETRY_MAX_ATTEMPTS),
            "block_retry_state": cfg.get("block_retry") or {},
        },
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
        view["not_configured"] = [k for k in settings.not_configured() if k.startswith("PLS_")]
        if s.source_name == "PakistanLawSite":
            view["citation_grid_progress"] = _citation_grid_progress_view(s.source_name, cfg)
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
    mode = await get_harvest_mode(db)
    return [
        await source_view(db, s, engine, mode=mode)
        for s in (await db.execute(select(ScraperSource).order_by(ScraperSource.source_name))).scalars().all()
    ]


@router.get("/harvest-mode")
async def harvest_mode_status(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    mode = await get_harvest_mode(db)
    rows = (await db.execute(select(ScraperSource).where(ScraperSource.is_active.is_(True)).order_by(ScraperSource.source_name))).scalars().all()
    selected = [s.source_name for s in rows if source_selected_for_mode(s, mode)]
    progress = await backfill_progress(db, source_names=selected if mode == "backfill" else None)
    metadata = {
        m.key: m.value
        for m in (
            await db.execute(
                select(CorpusMetadata).where(
                    CorpusMetadata.key.in_(
                        ["harvest_mode_changed_at", "harvest_mode_changed_by", "harvest_mode_reason"]
                    )
                )
            )
        ).scalars().all()
    }

    login_sources = [s for s in rows if s.access_method == "login_session" and source_selected_for_mode(s, mode)]
    login_readiness: List[Dict[str, Any]] = []
    for s in login_sources:
        slots = (
            await db.execute(
                select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == s.source_name).order_by(BrowserSessionSlot.slot_number)
            )
        ).scalars().all()
        login_readiness.append(
            {
                "source_name": s.source_name,
                "state": s.state,
                "slot_states": [{"slot": sl.slot_number, "state": sl.state, "reason": sl.state_reason} for sl in slots],
                "blocked": [sl.slot_number for sl in slots if sl.state in ("EMPTY", "NEEDS_HUMAN_LOGIN", "HALTED")],
            }
        )

    return {
        "mode": mode,
        "auto_switch": settings.HARVEST_AUTO_SWITCH,
        "selected_sources": selected,
        "dispatch_loop_seconds": settings.DISPATCH_LOOP_SECONDS,
        "update_defaults": {"cadence_hours": settings.UPDATE_CADENCE_HOURS},
        "backfill_defaults": {
            "source_frequency_minutes": settings.BACKFILL_SOURCE_FREQUENCY_MINUTES,
            "pages_per_hour": settings.BACKFILL_PAGES_PER_HOUR,
            "pages_per_day": settings.BACKFILL_PAGES_PER_DAY,
            "login_delay_min": settings.BACKFILL_LOGIN_DELAY_MIN,
            "login_delay_max": settings.BACKFILL_LOGIN_DELAY_MAX,
            "login_session_concurrency": settings.BACKFILL_LOGIN_SESSION_CONCURRENCY,
        },
        "progress": progress,
        "last_change": {
            "changed_at": metadata.get("harvest_mode_changed_at"),
            "changed_by": metadata.get("harvest_mode_changed_by"),
            "reason": metadata.get("harvest_mode_reason"),
        },
        "login_readiness": login_readiness,
    }


@router.post("/harvest-mode")
async def set_mode(body: HarvestModeBody, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    mode = body.mode.strip().lower()
    if mode not in ("backfill", "updates"):
        raise HTTPException(422, "mode must be backfill|updates")
    await set_harvest_mode(
        db,
        mode,  # type: ignore[arg-type]
        changed_by=(body.changed_by or "dashboard"),
        reason=(body.reason or ""),
    )
    if mode == "updates":
        now = datetime.now(timezone.utc)
        for s in (await db.execute(select(ScraperSource).where(ScraperSource.is_active.is_(True)))).scalars().all():
            if source_selected_for_mode(s, "updates"):
                s.next_scrape_at = now
    await db.commit()
    return await harvest_mode_status(db)


@router.get("/{name}/status")
async def status(name: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    return await source_view(db, await _source(db, name), mode=await get_harvest_mode(db))


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
        s.config_json = {**(s.config_json or {}), "paused_by_admin": True}
    elif body.action == "resume":
        if s.state == "HALTED":
            raise HTTPException(409, "a HALTED source needs re_enable with reviewed_by")
        s.state, s.state_reason = "ACTIVE", None
        s.config_json = {**(s.config_json or {}), "paused_by_admin": False}
    elif body.action == "retry_now":
        if s.state == "HALTED" and not body.reviewed_by:
            raise HTTPException(422, "retry_now on HALTED sources requires reviewed_by")
        s.state = "ACTIVE"
        s.state_reason = f"retry requested by {body.reviewed_by or 'admin'}: {body.reason or ''}".strip()
        s.requires_admin_review = False
        s.next_scrape_at = now
        cfg = dict(s.config_json or {})
        cfg.pop("block_retry", None)
        cfg["paused_by_admin"] = False
        s.config_json = cfg
        for sl in (
            await db.execute(
                select(BrowserSessionSlot).where(
                    BrowserSessionSlot.source_name == s.source_name, BrowserSessionSlot.state == "HALTED"
                )
            )
        ).scalars().all():
            sl.state, sl.state_reason = (
                "NEEDS_HUMAN_LOGIN",
                "retry requested after block review; fresh human login required",
            )
    elif body.action == "re_enable":
        if not body.reviewed_by:
            raise HTTPException(422, "re_enable requires reviewed_by (admin review of the block)")
        s.state, s.state_reason, s.requires_admin_review = "ACTIVE", f"re-enabled by {body.reviewed_by}: {body.reason or ''}", False
        s.config_json = {**(s.config_json or {}), "paused_by_admin": False}
        for sl in (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == s.source_name, BrowserSessionSlot.state == "HALTED"))).scalars().all():
            sl.state, sl.state_reason = "NEEDS_HUMAN_LOGIN", "re-enabled after block review; fresh human login required"
    elif body.action == "clear_block_retry":
        cfg = dict(s.config_json or {})
        cfg.pop("block_retry", None)
        s.config_json = cfg
        s.state_reason = body.reason or "block retry state cleared by admin"
    elif body.action == "disable":
        s.state, s.state_reason, s.is_active = "DISABLED", body.reason or "disabled by admin", False
    elif body.action == "enable":
        s.is_active = True
        s.state = "ACTIVE"
        s.state_reason = body.reason or "enabled by admin"
        s.requires_admin_review = False
        s.next_scrape_at = now
        s.config_json = {**(s.config_json or {}), "paused_by_admin": False}
    else:
        raise HTTPException(422, "action must be pause|resume|retry_now|re_enable|clear_block_retry|disable|enable")
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
    if body.update_frequency_hours is not None:
        cfg["update_frequency_hours"] = int(body.update_frequency_hours)
    if body.backfill_frequency_minutes is not None:
        cfg["backfill_frequency_minutes"] = int(body.backfill_frequency_minutes)
    if body.backfill_priority is not None:
        cfg["backfill_priority"] = int(body.backfill_priority)
    if body.backfill_enabled is not None:
        cfg["backfill_enabled"] = bool(body.backfill_enabled)
    if body.update_enabled is not None:
        cfg["update_enabled"] = bool(body.update_enabled)
    if body.auto_retry_on_block is not None:
        cfg["auto_retry_on_block"] = bool(body.auto_retry_on_block)
    if body.block_retry_cooldown_minutes is not None:
        cfg["block_retry_cooldown_minutes"] = int(body.block_retry_cooldown_minutes)
    if body.block_retry_max_attempts is not None:
        cfg["block_retry_max_attempts"] = int(body.block_retry_max_attempts)
    if cfg:
        s.config_json = cfg
    if body.is_active is not None:
        s.is_active = body.is_active
    await db.commit()
    return await source_view(db, s)
