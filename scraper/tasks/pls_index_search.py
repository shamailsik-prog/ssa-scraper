"""PakistanLawSite IndexSearch tick. Off unless PLS_INDEX_SEARCH_ENABLED is set.

One index page per tick, then the login is free again. Search harvest, including a
GAP_FOCUS row, is claimable work and this tick leaves the login to it. The cursor
and the day/month budgets live in the source config under pls_index_search.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery import shared_task
from sqlalchemy import select

from scraper.auth.session_manager import LoginRequired, SessionLock, SessionLockHeld, merge_source_config, release_page_result
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.harvest_mode import get_harvest_mode, login_pacing_profile
from scraper.models import ScraperSource
from scraper.pls_index_search import (
    INDEX_SEARCH_STATE_KEY,
    IndexSearchChallenge,
    index_search_should_yield,
    limits_from_settings,
    queries_from_settings,
    run_index_search,
)
from scraper.security import ExplicitBlock, VerificationRequired
from scraper.tasks.pakistanlawsite import (
    PacingBudgetExceeded,
    PakistanLawSitePipeline,
    SOURCE_NAME,
    claim_free_slot,
    release_claimed_slot,
)
from scraper.tasks.pls_search_harvest import SEARCH_HARVEST_SLOTS, load_known_citation_keys, pick_next_query

logger = logging.getLogger(__name__)

TASK_NAME = "scraper.tasks.pls_index_search.pls_index_search_tick"


def index_search_beat_entries() -> dict:
    """Beat entry for this tick. Empty while the flag is off, so a deploy changes nothing."""
    if not settings.PLS_INDEX_SEARCH_ENABLED:
        return {}
    schedule = int(settings.PLS_INDEX_SEARCH_SCHEDULE_SECONDS)
    return {
        "pls-index-search": {
            "task": TASK_NAME,
            "schedule": schedule,
            "options": {"expires": schedule},
        }
    }


async def index_search_yield_reason(db, *, session_lock_held: bool) -> Optional[str]:
    """search_harvest when either login's next ledger row is claimable, including GAP_FOCUS."""
    harvest_claimable = False
    for slot in SEARCH_HARVEST_SLOTS:
        if await pick_next_query(db, slot=slot) is not None:
            harvest_claimable = True
            break
    return index_search_should_yield(harvest_claimable=harvest_claimable, session_lock_held=session_lock_held)


async def run_index_search_tick(db, *, redis_client=None) -> Dict[str, Any]:
    if not settings.PLS_INDEX_SEARCH_ENABLED:
        return {"skipped": "PLS_INDEX_SEARCH_ENABLED is off"}
    if not settings.login_scraping_effective:
        return {"skipped": "login scraping not permitted in this environment"}
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    if source is None:
        return {"skipped": "PakistanLawSite source missing"}
    if source.state in ("HALTED", "DISABLED", "PAUSED") or not source.is_active:
        return {"skipped": f"source state {source.state}"}
    reason = await index_search_yield_reason(db, session_lock_held=False)
    if reason:
        return {"skipped": reason}

    now = datetime.now(timezone.utc)
    cfg = dict(source.config_json or {})
    state = dict(cfg.get(INDEX_SEARCH_STATE_KEY) or {})
    pipeline = PakistanLawSitePipeline(db, source, redis_client=redis_client)
    mode = await get_harvest_mode(db)
    pipeline.harvest_mode = mode
    pipeline.pacing_profile = login_pacing_profile(mode)
    active = len([slot for slot in await pipeline.manager.slots() if slot.state == "ACTIVE"])
    lock = SessionLock(
        SOURCE_NAME,
        redis_client,
        max_holders=max(1, min(int(pipeline.pacing_profile.get("login_session_concurrency") or 1), active or 1)),
    )
    own = SessionLock(f"{SOURCE_NAME}:index_search", redis_client, max_holders=1)
    try:
        await own.acquire()
    except SessionLockHeld:
        await own.release()
        return {"skipped": "another index search is running"}
    try:
        await lock.acquire()
    except SessionLockHeld:
        await lock.release()
        await own.release()
        return {"skipped": "login_session_busy"}
    try:
        pipeline._session_lock = lock
        pipeline._run_locks.append(own)
        if await claim_free_slot(pipeline) is None:
            return {"skipped": "no free login slot"}
        pipeline._assert_permitted()

        async def fetch_index(request: Dict[str, Any]) -> str:
            try:
                page = await pipeline.fetch_index_surface(request)
            except (LoginRequired, VerificationRequired, ExplicitBlock) as exc:
                raise IndexSearchChallenge(str(exc)) from exc
            html = page.html or ""
            release_page_result(page)
            return html

        async def fetch_detail(url: str):
            try:
                return await pipeline.fetch_detail(url)
            except (LoginRequired, VerificationRequired, ExplicitBlock) as exc:
                raise IndexSearchChallenge(str(exc)) from exc

        async def preserve(detail, route, row):
            try:
                return await pipeline.preserve_and_extract(detail, route, row)
            finally:
                release_page_result(detail)

        async def known(keys):
            return await load_known_citation_keys(db, set(keys))

        result = await run_index_search(
            state=state,
            now=now,
            limits=limits_from_settings(),
            queries=queries_from_settings(now),
            fetch_index=fetch_index,
            fetch_detail=fetch_detail,
            preserve=preserve,
            known_keys=known,
            sleep=pipeline.sleep,
            monotonic=time.monotonic,
            rng=random.Random(),
            harvest_claimable=False,
        )
        await merge_source_config(db, source, {INDEX_SEARCH_STATE_KEY: result["state"]})
        await db.commit()
        logger.info(
            "PakistanLawSite IndexSearch tick stopped=%s staged=%s known=%s reporter=%s year=%s page=%s",
            result.get("stopped"),
            result.get("staged"),
            result.get("known"),
            (result.get("state") or {}).get("reporter"),
            (result.get("state") or {}).get("year"),
            (result.get("state") or {}).get("page"),
        )
        return result
    except PacingBudgetExceeded as exc:
        await db.rollback()
        logger.info("PakistanLawSite IndexSearch paused on the login budget: %s", exc)
        return {"stopped": "login_pacing"}
    finally:
        try:
            await pipeline.runner.close()
        except Exception:
            logger.debug("IndexSearch runner close failed", exc_info=True)
        await release_claimed_slot(pipeline)
        await lock.release()
        await own.release()


@shared_task(name=TASK_NAME)
def pls_index_search_tick(**kwargs) -> Dict[str, Any]:
    if not settings.PLS_INDEX_SEARCH_ENABLED:
        return {"skipped": "PLS_INDEX_SEARCH_ENABLED is off"}
    from scraper.tasks.deploy_hold import login_work_held

    if login_work_held():
        return {"skipped": "deploy_hold"}

    async def _inner() -> Dict[str, Any]:
        from scraper.heartbeat import beat

        await beat("index_search")
        async with SessionLocal() as db:
            return await run_index_search_tick(db)

    return run_async(_inner())
