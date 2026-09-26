"""Celery periodic tasks: PLS slot keepalive and citation-grid stall watchdog."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from celery import shared_task
from sqlalchemy import select, update

from scraper.auth.session_manager import SessionLock, SessionLockHeld, SessionManager, merge_source_config
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.models import BrowserSessionSlot, CrawlFrontier, ScraperSource
from scraper.notify import notify
from scraper.pls_grid_health import (
    SOURCE_NAME,
    grid_harvest_incomplete,
    grid_rows_remaining,
    pls_harvest_in_progress,
    pls_judgment_counts,
    pls_last_judgment_at,
    pls_source_config,
    signatures_equal,
    stall_signature,
)
from scraper.tasks.login_recovery import recover_slot, verify_stored_session
from scraper.auth.session_manager import playwright_browser_factory

logger = logging.getLogger(__name__)

WATCHDOG_META_KEY = "pls_stall_watchdog"
STALL_AFTER = timedelta(hours=2)


async def keepalive_pakistanlawsite_slots() -> Dict[str, Any]:
    """Probe ACTIVE slots; re-login only slots that fail the live search probe."""
    if not settings.login_scraping_effective:
        return {"skipped": "login_scraping_disabled"}
    outcomes: Dict[str, Any] = {"probed": [], "recovered": []}
    async with SessionLocal() as db:
        busy = await pls_harvest_in_progress(db)
        if busy:
            return {"skipped": "harvest_in_progress", "reason": busy}
        source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
        if source is None:
            return {"skipped": "no source"}
        manager = SessionManager(db, source)
        slots = await manager.slots()
        for slot in slots:
            if slot.state == "ACTIVE":
                lock = SessionLock(source.source_name)
                try:
                    await lock.acquire()
                except SessionLockHeld:
                    outcomes["probed"].append({"slot": slot.slot_number, "skipped": "login_session_lock_held"})
                    continue
                try:
                    check = await verify_stored_session(manager, slot, playwright_browser_factory)
                finally:
                    await lock.release()
                alive = bool(check.get("alive"))
                outcomes["probed"].append({"slot": slot.slot_number, "alive": alive, "verdict": check.get("verdict")})
                if alive:
                    continue
                await manager.mark_needs_human_login(slot.slot_number, "keepalive probe failed")
                await db.flush()
            if slot.state in ("NEEDS_HUMAN_LOGIN", "EMPTY"):
                rec = await recover_slot(db, manager, slot)
                outcomes["recovered"].append({"slot": slot.slot_number, **rec})
        await db.commit()
    return outcomes


async def stall_watchdog_pakistanlawsite() -> Dict[str, Any]:
    if not settings.login_scraping_effective:
        return {"skipped": "login_scraping_disabled"}
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
        if source is None:
            return {"skipped": "no source"}
        cfg = dict(source.config_json or {})
        if not grid_harvest_incomplete(cfg):
            await merge_source_config(db, source, {WATCHDOG_META_KEY: {"stalled": False, "checked_at": now.isoformat()}})
            await db.commit()
            return {"stalled": False, "reason": "grid complete"}
        total_j, _ = await pls_judgment_counts(db)
        sig = stall_signature(cfg, total_j)
        watch = dict(cfg.get(WATCHDOG_META_KEY) or {})
        prev_sig = watch.get("signature")
        prev_at = watch.get("checked_at")
        stalled = False
        if prev_sig is not None and signatures_equal(prev_sig, sig):
            try:
                prev_dt = datetime.fromisoformat(str(prev_at))
                if prev_dt.tzinfo is None:
                    prev_dt = prev_dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                prev_dt = now - STALL_AFTER
            if now - prev_dt >= STALL_AFTER:
                stalled = True
        else:
            watch = {"signature": sig, "checked_at": now.isoformat(), "stalled": False}
        if stalled:
            logger.warning(
                "PLS STALL WATCHDOG: citation grid has %s rows remaining but cursor/judgments unchanged for %s hours",
                grid_rows_remaining(cfg),
                int(STALL_AFTER.total_seconds() // 3600),
            )
            manager = SessionManager(db, source)
            for slot in await manager.slots():
                if slot.state == "ACTIVE":
                    lock = SessionLock(source.source_name)
                    try:
                        await lock.acquire()
                    except SessionLockHeld:
                        watch.setdefault("recovery", []).append(
                            {"slot": slot.slot_number, "skipped": "login_session_lock_held"}
                        )
                        continue
                    try:
                        check = await verify_stored_session(manager, slot, playwright_browser_factory)
                    finally:
                        await lock.release()
                    if not check.get("alive"):
                        await manager.mark_needs_human_login(slot.slot_number, "stall watchdog probe failed")
                rec = await recover_slot(db, manager, slot)
                watch.setdefault("recovery", []).append(rec)
            from scraper.tasks.celery_app import app

            app.send_task("scraper.tasks.dispatcher.run_login_session_job", args=(SOURCE_NAME,), queue="login_session")
            await notify(
                db,
                level="warning",
                code="PLS_HARVEST_STALLED",
                message=f"PakistanLawSite citation grid stalled with {grid_rows_remaining(cfg)} rows remaining; slots probed and harvest requeued",
                source_name=SOURCE_NAME,
            )
            watch["stalled"] = True
            watch["stalled_at"] = now.isoformat()
        else:
            watch["signature"] = sig
            watch["checked_at"] = now.isoformat()
            watch["stalled"] = False
        await merge_source_config(db, source, {WATCHDOG_META_KEY: watch})
        await db.commit()
        return {"stalled": stalled, "rows_remaining": grid_rows_remaining(cfg), "judgments": total_j}


async def reset_retired_pls_search_map_frontier_db(db) -> int:
    pattern = "%search map cannot express%"
    result = await db.execute(
        update(CrawlFrontier)
        .where(
            CrawlFrontier.source_name == SOURCE_NAME,
            CrawlFrontier.status == "retired",
            CrawlFrontier.last_error.ilike(pattern),
        )
        .values(status="pending", last_error=None)
    )
    return int(result.rowcount or 0)


async def reset_retired_pls_search_map_frontier() -> Dict[str, int]:
    async with SessionLocal() as db:
        reset = await reset_retired_pls_search_map_frontier_db(db)
        await db.commit()
        return {"reset": reset}


@shared_task(name="scraper.tasks.pls_self_healing.pls_keepalive_hourly")
def pls_keepalive_hourly():
    return run_async(keepalive_pakistanlawsite_slots())


@shared_task(name="scraper.tasks.pls_self_healing.pls_stall_watchdog")
def pls_stall_watchdog():
    return run_async(stall_watchdog_pakistanlawsite())
