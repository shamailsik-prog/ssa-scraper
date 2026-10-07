"""Celery periodic tasks: PLS slot keepalive and citation-grid stall watchdog."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from celery import shared_task
from sqlalchemy import func, or_, select, update

from scraper.auth.session_manager import SessionLock, SessionLockHeld, SessionManager, merge_source_config
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.models import (
    RETIRED_WITHOUT_REASON,
    BrowserSessionSlot,
    CrawlFrontier,
    Notification,
    PlsSearchHarvestQuery,
    ScraperSource,
)
from scraper.notify import notify
from scraper.pls_grid_health import (
    SOURCE_NAME,
    compute_stalled,
    defer_pls_grid_for_search_harvest,
    grid_harvest_incomplete,
    grid_saturation_view,
    parse_iso,
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


async def evaluate_pls_stall(db, source: ScraperSource, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Compute the stalled alarm from the corpus itself and notify once per episode.

    stalled == no PakistanLawSite judgment promoted for PLS_STALL_NO_PROMOTION_HOURS. Cursor movement is
    deliberately ignored: the citation grid advanced every window for 31 hours (2026-09-28 15:26 UTC
    to 2026-09-30) while nothing was promoted, and the old cursor-signature watchdog never fired."""
    now = now or datetime.now(timezone.utc)
    cfg = dict(source.config_json or {})
    last_at = await pls_last_judgment_at(db)
    saturation = grid_saturation_view(cfg, now=now)
    paused = source.state != "ACTIVE" or bool(cfg.get("paused_by_admin"))
    verdict = compute_stalled(last_promotion_at=last_at, now=now, saturation=saturation, harvest_paused=paused)
    watch = dict(cfg.get(WATCHDOG_META_KEY) or {})
    was_stalled = bool(watch.get("stalled"))
    watch.update({k: verdict[k] for k in ("stalled", "stalled_reason", "hours_since_last_judgment", "threshold_hours")})
    watch["checked_at"] = now.isoformat()
    if verdict["stalled"] and not was_stalled:
        watch["stalled_at"] = now.isoformat()
        open_alarm = (
            await db.execute(
                select(Notification).where(Notification.code == "PLS_NO_NEW_JUDGMENTS", Notification.acknowledged.is_(False)).limit(1)
            )
        ).scalars().first()
        if open_alarm is None:
            await notify(
                db,
                level="warning" if verdict["stalled_reason"] != "harvest_paused" else "info",
                code="PLS_NO_NEW_JUDGMENTS",
                message=(
                    f"PakistanLawSite promoted no new judgment for {verdict['hours_since_last_judgment']} h "
                    f"(threshold {verdict['threshold_hours']} h; reason: {verdict['stalled_reason']})"
                ),
                source_name=SOURCE_NAME,
                details={**verdict, "saturation": saturation},
            )
    if not verdict["stalled"] and was_stalled:
        watch.pop("stalled_at", None)
        watch["recovered_at"] = now.isoformat()
        for n in (
            await db.execute(select(Notification).where(Notification.code == "PLS_NO_NEW_JUDGMENTS", Notification.acknowledged.is_(False)))
        ).scalars().all():
            n.acknowledged = True
    await merge_source_config(db, source, {WATCHDOG_META_KEY: watch})
    return verdict


async def stall_watchdog_pakistanlawsite() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
        if source is None:
            return {"skipped": "no source"}
        verdict = await evaluate_pls_stall(db, source, now=now)
        await db.commit()
        result: Dict[str, Any] = dict(verdict)
        if not settings.login_scraping_effective:
            result["recovery"] = "login_scraping_disabled"
            return result
        cfg = dict(source.config_json or {})
        if verdict["stalled_reason"] == "harvest_paused":
            return result
        if verdict["stalled_reason"] not in ("no_output_while_harvesting", "grid_saturated"):
            return result
        if verdict["stalled_reason"] == "no_output_while_harvesting" and not grid_harvest_incomplete(cfg):
            return result
        total_j, _ = await pls_judgment_counts(db)
        sig = stall_signature(cfg, total_j)
        watch = dict(cfg.get(WATCHDOG_META_KEY) or {})
        prev_sig = watch.get("signature")
        prev_dt = parse_iso(watch.get("signature_at"))
        # A watch written before signature_at existed starts its frozen episode now, or it never arms.
        if prev_sig is None or not signatures_equal(prev_sig, sig) or prev_dt is None:
            watch["signature"], watch["signature_at"] = sig, now.isoformat()
            await merge_source_config(db, source, {WATCHDOG_META_KEY: watch})
            await db.commit()
            return result
        if prev_dt is None or now - prev_dt < STALL_AFTER:
            return result
        # Real hang: harvest "running" with cursors AND judgments frozen. Probe the slots and requeue.
        logger.warning(
            "PLS STALL WATCHDOG: %s grid rows remaining but cursors and judgments unchanged for %s hours",
            grid_rows_remaining(cfg),
            int(STALL_AFTER.total_seconds() // 3600),
        )
        manager = SessionManager(db, source)
        recovery: list = []
        for slot in await manager.slots():
            if slot.state == "ACTIVE":
                lock = SessionLock(source.source_name)
                try:
                    await lock.acquire()
                except SessionLockHeld:
                    recovery.append({"slot": slot.slot_number, "skipped": "login_session_lock_held"})
                    continue
                try:
                    check = await verify_stored_session(manager, slot, playwright_browser_factory)
                finally:
                    await lock.release()
                if not check.get("alive"):
                    await manager.mark_needs_human_login(slot.slot_number, "stall watchdog probe failed")
            recovery.append(await recover_slot(db, manager, slot))
        from scraper.tasks.celery_app import app

        pending_gaps = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PlsSearchHarvestQuery)
                    .where(
                        PlsSearchHarvestQuery.source_name == SOURCE_NAME,
                        PlsSearchHarvestQuery.status.in_(("pending", "in_progress")),
                    )
                )
            ).scalar()
            or 0
        )
        if pending_gaps > 0 and defer_pls_grid_for_search_harvest(cfg, stall_reason=verdict.get("stalled_reason")):
            app.send_task(
                "scraper.tasks.pls_search_harvest.pls_search_harvest_tick",
                kwargs={"priority_gaps": True},
                queue="login_session",
            )
        elif verdict["stalled_reason"] == "no_output_while_harvesting" and grid_harvest_incomplete(cfg):
            app.send_task("scraper.tasks.dispatcher.run_login_session_job", args=(SOURCE_NAME,), queue="login_session")
        watch["recovery"], watch["signature_at"] = recovery, now.isoformat()
        await merge_source_config(db, source, {WATCHDOG_META_KEY: watch})
        await db.commit()
        result["recovery"] = recovery
        return result


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


async def reset_retired_pls_frontier_without_reason_db(db, *, dry_run: bool = False) -> int:
    """Retired rows that were never run (last_run_at null) and carry no last_error were not retired by the crawler.
    Put them back to pending; the model guard makes sure this can no longer be produced by the ORM."""
    cond = (
        CrawlFrontier.source_name == SOURCE_NAME,
        CrawlFrontier.status == "retired",
        or_(CrawlFrontier.last_error.is_(None), CrawlFrontier.last_error == "", CrawlFrontier.last_error == RETIRED_WITHOUT_REASON),
        CrawlFrontier.last_run_at.is_(None),
    )
    if dry_run:
        return int((await db.execute(select(func.count()).select_from(CrawlFrontier).where(*cond))).scalar() or 0)
    result = await db.execute(update(CrawlFrontier).where(*cond).values(status="pending", last_error=None, attempts=0))
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


def _pls_keepalive_cli() -> int:
    result = run_async(keepalive_pakistanlawsite_slots())
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(_pls_keepalive_cli())
