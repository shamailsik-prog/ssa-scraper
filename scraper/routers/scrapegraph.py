"""
/admin/scrapegraph — ScrapeGraph observability (Amendment §18).

    GET  /admin/scrapegraph/status        engines, breakers, budget, schema, NOT CONFIGURED items
    POST /admin/scrapegraph/test-public   extracts the configured PUBLIC test URL only
    GET  /admin/scrapegraph/usage         daily ledger by engine and source

The test endpoint never accepts arbitrary HTML or an arbitrary URL from the browser.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import get_db
from scraper.extractors.prompts import prompt_fingerprint
from scraper.extractors.schemas import SCHEMA_VERSION
from scraper.extractors.scrapegraph_base import LOCAL_BREAKER, MANAGED_BREAKER, managed_credits_used_today
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.extractors.scrapegraph_managed import ManagedScrapeGraphEngine
from scraper.models import ScraperSource, SgaiUsageDaily
from scraper.routers.auth import require_admin

router = APIRouter(prefix="/admin/scrapegraph", tags=["scrapegraph"], dependencies=[Depends(require_admin)])


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    local = LocalScrapeGraphEngine()
    used = await managed_credits_used_today(db)
    return {
        "enabled": settings.SGAI_ENABLED,
        "mode_default": settings.SGAI_MODE,
        "schema_version": settings.SGAI_SCHEMA_VERSION,
        "schema_version_code": SCHEMA_VERSION,
        "prompt_fingerprints": {k: prompt_fingerprint(k) for k in ("judgment", "statute", "instrument", "result_rows", "search_form_map")},
        "managed": {
            "api_key": "CONFIGURED" if settings.SGAI_API_KEY.get_secret_value() else "NOT CONFIGURED",
            "public_only": settings.SGAI_MANAGED_PUBLIC_ONLY,
            "stealth_allowed": settings.SGAI_STEALTH_ALLOWED,
            "circuit_breaker": "open" if MANAGED_BREAKER.is_open else "closed",
            "consecutive_failures": MANAGED_BREAKER.failures,
            "daily_credit_cap": settings.SGAI_DAILY_CREDIT_CAP if settings.SGAI_DAILY_CREDIT_CAP is not None else "NOT CONFIGURED",
            "credits_used_today": used,
            "budget_exhausted": settings.SGAI_DAILY_CREDIT_CAP is not None and used >= settings.SGAI_DAILY_CREDIT_CAP,
            "timeout_seconds": settings.SGAI_TIMEOUT_SECONDS,
            "max_retries": settings.SGAI_MAX_RETRIES,
        },
        "local": {"status": local.status, "provider": local.provider, "model": local.model or "NOT CONFIGURED", "base_url": local.base_url or "NOT CONFIGURED", "circuit_breaker": "open" if LOCAL_BREAKER.is_open else "closed", "health": await local.health()},
        "cache_enabled": settings.SGAI_CACHE_ENABLED,
        "fail_open_to_deterministic": settings.SGAI_FAIL_OPEN_TO_DETERMINISTIC,
        "public_test_url": settings.SGAI_PUBLIC_TEST_URL or "NOT CONFIGURED",
        "not_configured": [k for k in settings.not_configured() if k.startswith("SGAI")],
        "mcp": "development/operator tooling only; not a runtime dependency (see docs/MCP_DEVELOPMENT.md)",
    }


@router.post("/test-public")
async def test_public(extraction_type: str = Query(default="judgment", pattern="^(judgment|statute|instrument|result_rows)$"), db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    url = settings.SGAI_PUBLIC_TEST_URL
    if not url:
        raise HTTPException(409, "SGAI_PUBLIC_TEST_URL is NOT CONFIGURED")
    engine = ManagedScrapeGraphEngine()
    if not engine.configured:
        raise HTTPException(409, "SGAI_API_KEY is NOT CONFIGURED")
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    sources = (await db.execute(select(ScraperSource).where(ScraperSource.access_method == "public"))).scalars().all()
    allow: List[str] = [h for s in sources for h in (s.allow_list or [])]
    if not any(host == h or host.endswith("." + h) for h in allow):
        raise HTTPException(409, f"test URL host {host} is not in any PUBLIC source allow-list")
    from scraper.extractors.scrapegraph_base import ExtractionInput
    from scraper.fetchers import sha256_text

    inp = ExtractionInput(source_name="public-test", access_method="public", content_hash=sha256_text(url), url=url)
    result = await engine.extract(extraction_type, inp)
    return {"url": url, "status": result.status, "engine": result.engine_mode, "elapsed_ms": result.elapsed_ms, "error": result.error, "data": result.data}


@router.get("/usage")
async def usage(days: int = Query(default=7, ge=1, le=90), db: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    rows = (await db.execute(select(SgaiUsageDaily).where(SgaiUsageDaily.day >= since).order_by(SgaiUsageDaily.day.desc(), SgaiUsageDaily.engine_mode, SgaiUsageDaily.source_name))).scalars().all()
    return [{"day": r.day.isoformat(), "engine": r.engine_mode, "source": r.source_name, "calls": r.calls, "credits": r.credits, "cache_hits": r.cache_hits, "successes": r.successes, "failures": r.failures, "conflicts": r.conflicts} for r in rows]
