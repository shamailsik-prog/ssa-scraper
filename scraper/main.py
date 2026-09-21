"""
FastAPI application — the corpus service API and operator dashboard.

Start-up: refuses to run without ENCRYPTION_KEY and ADMIN_API_KEY, initialises the database
(extensions, migrations, roles, seed, corpus_metadata), and never logs or returns a secret.
"""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text

from scraper.config import settings
from scraper.database import SessionLocal, embedding_identity_matches, engine, init_db
from scraper.harvest_layers import describe_layers
from scraper.harvest_mode import backfill_progress, get_harvest_mode, selected_source_names
from scraper.routers import archive, corpus, coverage, export, jobs, review, scrapegraph, search, sessions, sources

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scraper.main")

STARTUP_STATE: Dict[str, Any] = {"db": None, "error": None}


def preflight() -> None:
    missing = [k for k in ("ENCRYPTION_KEY", "ADMIN_API_KEY") if not getattr(settings, k)]
    if missing:
        raise RuntimeError(f"refusing to start: {', '.join(missing)} not configured")


@asynccontextmanager
async def lifespan(app: FastAPI):
    preflight()
    try:
        STARTUP_STATE["db"] = await init_db()
    except Exception as exc:  # keep the API up so /health can report the failure
        STARTUP_STATE["error"] = str(exc)[:500]
        logger.exception("database initialisation failed")
    yield
    await engine.dispose()


app = FastAPI(title=settings.PROJECT_NAME, version="D-15", lifespan=lifespan, docs_url="/docs" if settings.DEBUG else None, redoc_url=None)
for r in (sources, coverage, corpus, review, export, sessions, archive, scrapegraph, jobs, search):
    app.include_router(r.router)


@app.get("/health")
async def health() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "environment": settings.ENVIRONMENT,
        "deploy_region": settings.DEPLOY_REGION or "NOT CONFIGURED",
        "login_scraping_permitted": settings.login_scraping_effective,
        "startup": STARTUP_STATE,
        "not_configured": settings.not_configured(),
        # Firm LLM keys in not_configured are optional enrichment; they do not block scrape.
        "not_configured_note": "SGAI_*/OPENAI_API_KEY NOT CONFIGURED = optional enrichment only; deterministic harvest continues.",
    }
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
            from scraper.models import Judgment, QuarantineQueue, ScraperJob, ScraperSource, Statute, StatuteSection

            out["db_connected"] = True
            out["judgments"] = (await db.execute(select(func.count()).select_from(Judgment))).scalar()
            out["statutes"] = (await db.execute(select(func.count()).select_from(Statute))).scalar()
            out["statute_sections"] = (await db.execute(select(func.count()).select_from(StatuteSection))).scalar()
            out["review_queue_open"] = (await db.execute(select(func.count()).select_from(QuarantineQueue).where(QuarantineQueue.reviewed.is_(False)))).scalar()
            out["sources"] = {s.source_name: s.state for s in (await db.execute(select(ScraperSource))).scalars().all()}
            out["running_jobs"] = (await db.execute(select(func.count()).select_from(ScraperJob).where(ScraperJob.status == "running"))).scalar()
            out["embedding_identity_ok"] = await embedding_identity_matches()
            harvest_mode = await get_harvest_mode(db)
            out["harvest_mode"] = harvest_mode
            out["harvest_layers"] = describe_layers(harvest_mode)
            out["backfill_progress"] = await backfill_progress(
                db, source_names=(await selected_source_names(db, "backfill"))
            )
            out["read_only_role"] = {
                "name": settings.SIKANDER_READER_ROLE,
                "status": "CONFIGURED" if settings.reader_role_configured else "NOT CONFIGURED",
                "exists": bool((await db.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": settings.SIKANDER_READER_ROLE})).scalar()),
            }
    except Exception as exc:
        out["status"] = "degraded"
        out["db_connected"] = False
        out["error"] = str(exc)[:300]
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings.REDIS_URL)
        out["redis_connected"] = bool(await r.ping())
        await r.aclose()
    except Exception as exc:
        out["redis_connected"] = False
        out["status"] = "degraded"
        out["redis_error"] = str(exc)[:200]
    return out


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    p = pathlib.Path(__file__).parent / "templates" / "dashboard.html"
    return p.read_text(encoding="utf-8")


@app.get("/")
async def root() -> Dict[str, str]:
    return {"service": settings.PROJECT_NAME, "dashboard": "/dashboard", "health": "/health"}
