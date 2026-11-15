"""Advance Search and Smart Search ticks. Off unless their flags are set.

One listing page per tick, then the login is free again. Search harvest, including a
GAP_FOCUS row, is claimable work and this tick leaves the login to it. The cursor
and the day/month budgets live in the source config. With the flag off the tick
returns before it touches the database, and Beat has no entry.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from celery import shared_task
from sqlalchemy import select

from scraper.auth.session_manager import (
    LoginRequired,
    SessionLock,
    SessionLockHeld,
    merge_source_config,
    release_page_result,
)
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.harvest_mode import get_harvest_mode, login_pacing_profile
from scraper.models import ScraperSource
from scraper.pls_advance_search import (
    ADVANCE_SEARCH_STATE_KEY,
    advance_queries_from_settings,
    advance_request,
    limits_from_settings as advance_limits,
    parse_advance_page,
)
from scraper.pls_case_enumeration import EnumerationChallenge, run_case_enumeration
from scraper.pls_smart_search import (
    SMART_SEARCH_STATE_KEY,
    limits_from_settings as smart_limits,
    parse_smart_search,
    smart_queries_from_settings,
    smart_request,
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

ADVANCE_TASK = "scraper.tasks.pls_case_enumeration.pls_advance_search_tick"
SMART_TASK = "scraper.tasks.pls_case_enumeration.pls_smart_search_tick"


@dataclass(frozen=True)
class _Kind:
    harvest: str
    flag: str
    schedule_attr: str
    state_key: str
    beat_name: str
    task_name: str
    lock_suffix: str
    fetch_attr: str
    label: str


ADVANCE = _Kind(
    harvest="advance_search",
    flag="PLS_ADVANCE_SEARCH_ENABLED",
    schedule_attr="PLS_ADVANCE_SEARCH_SCHEDULE_SECONDS",
    state_key=ADVANCE_SEARCH_STATE_KEY,
    beat_name="pls-advance-search",
    task_name=ADVANCE_TASK,
    lock_suffix="advance_search",
    fetch_attr="fetch_advance_surface",
    label="Advance Search",
)
SMART = _Kind(
    harvest="smart_search",
    flag="PLS_SMART_SEARCH_ENABLED",
    schedule_attr="PLS_SMART_SEARCH_SCHEDULE_SECONDS",
    state_key=SMART_SEARCH_STATE_KEY,
    beat_name="pls-smart-search",
    task_name=SMART_TASK,
    lock_suffix="smart_search",
    fetch_attr="fetch_smart_surface",
    label="Smart Search",
)


def case_enumeration_beat_entries() -> dict:
    """Beat entries for the flags that are on. Empty while both are off."""
    entries = {}
    for kind in (ADVANCE, SMART):
        if not getattr(settings, kind.flag):
            continue
        schedule = int(getattr(settings, kind.schedule_attr))
        entries[kind.beat_name] = {
            "task": kind.task_name,
            "schedule": schedule,
            "options": {"expires": schedule},
        }
    return entries


def _smart_build_request(query, state):
    return smart_request(query, state, max_chars=int(settings.PLS_SMART_SEARCH_QUERY_MAX_CHARS))


async def enumeration_yield_reason(db, *, session_lock_held: bool) -> Optional[str]:
    """search_harvest when either login's next ledger row is claimable, including GAP_FOCUS."""
    if session_lock_held:
        return "login_session_busy"
    for slot in SEARCH_HARVEST_SLOTS:
        if await pick_next_query(db, slot=slot) is not None:
            return "search_harvest"
    return None


def _configured_queries(kind: _Kind):
    if kind is ADVANCE:
        return advance_queries_from_settings()
    return smart_queries_from_settings()


async def _run_tick(db, kind: _Kind, *, redis_client=None) -> Dict[str, Any]:
    if not getattr(settings, kind.flag):
        return {"skipped": f"{kind.flag} is off"}
    if not settings.login_scraping_effective:
        return {"skipped": "login scraping not permitted in this environment"}
    # A flipped flag with no queries must not take the login.
    queries = _configured_queries(kind)
    if not queries:
        return {"skipped": "no_queries"}
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    if source is None:
        return {"skipped": "PakistanLawSite source missing"}
    if source.state in ("HALTED", "DISABLED", "PAUSED") or not source.is_active:
        return {"skipped": f"source state {source.state}"}
    reason = await enumeration_yield_reason(db, session_lock_held=False)
    if reason:
        return {"skipped": reason}

    now = datetime.now(timezone.utc)
    cfg = dict(source.config_json or {})
    state = dict(cfg.get(kind.state_key) or {})
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
    own = SessionLock(f"{SOURCE_NAME}:{kind.lock_suffix}", redis_client, max_holders=1)
    try:
        await own.acquire()
    except SessionLockHeld:
        await own.release()
        return {"skipped": f"another {kind.label} is running"}
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
        fetch_surface = getattr(pipeline, kind.fetch_attr)

        async def fetch_listing(request: Dict[str, Any]) -> str:
            try:
                page = await fetch_surface(request)
            except (LoginRequired, VerificationRequired, ExplicitBlock) as exc:
                raise EnumerationChallenge(str(exc)) from exc
            html = page.html or ""
            release_page_result(page)
            return html

        async def fetch_detail(url: str):
            try:
                return await pipeline.fetch_detail(url)
            except (LoginRequired, VerificationRequired, ExplicitBlock) as exc:
                raise EnumerationChallenge(str(exc)) from exc

        async def preserve(detail, route, row):
            try:
                return await pipeline.preserve_and_extract(detail, route, row)
            finally:
                release_page_result(detail)

        async def known(keys):
            return await load_known_citation_keys(db, set(keys))

        async def persist(snapshot):
            await merge_source_config(db, source, {kind.state_key: snapshot})
            await db.commit()

        if kind is ADVANCE:
            limits = advance_limits()
            build_request: Callable = advance_request
            parse_listing = parse_advance_page
        else:
            limits = smart_limits()
            build_request = _smart_build_request
            parse_listing = parse_smart_search

        result = await run_case_enumeration(
            harvest=kind.harvest,
            state=state,
            now=now,
            limits=limits,
            queries=queries,
            build_request=build_request,
            parse_listing=parse_listing,
            fetch_listing=fetch_listing,
            fetch_detail=fetch_detail,
            preserve=preserve,
            known_keys=known,
            sleep=pipeline.sleep,
            monotonic=time.monotonic,
            rng=random.Random(),
            harvest_claimable=False,
            persist=persist,
        )
        await merge_source_config(db, source, {kind.state_key: result["state"]})
        await db.commit()
        logger.info(
            "PakistanLawSite %s tick stopped=%s staged=%s known=%s query_index=%s page=%s",
            kind.label,
            result.get("stopped"),
            result.get("staged"),
            result.get("known"),
            (result.get("state") or {}).get("query_index"),
            (result.get("state") or {}).get("page"),
        )
        return result
    except PacingBudgetExceeded as exc:
        await db.rollback()
        logger.info("PakistanLawSite %s paused on the login budget: %s", kind.label, exc)
        return {"stopped": "login_pacing"}
    finally:
        try:
            await pipeline.runner.close()
        except Exception:
            logger.debug("%s runner close failed", kind.label, exc_info=True)
        await release_claimed_slot(pipeline)
        await lock.release()
        await own.release()


async def run_advance_search_tick(db, *, redis_client=None) -> Dict[str, Any]:
    return await _run_tick(db, ADVANCE, redis_client=redis_client)


async def run_smart_search_tick(db, *, redis_client=None) -> Dict[str, Any]:
    return await _run_tick(db, SMART, redis_client=redis_client)


def _sync_tick(kind: _Kind) -> Dict[str, Any]:
    if not getattr(settings, kind.flag):
        return {"skipped": f"{kind.flag} is off"}
    from scraper.tasks.deploy_hold import login_work_held

    if login_work_held():
        return {"skipped": "deploy_hold"}

    async def _inner() -> Dict[str, Any]:
        from scraper.heartbeat import beat

        await beat(kind.harvest)
        async with SessionLocal() as db:
            return await _run_tick(db, kind)

    return run_async(_inner())


@shared_task(name=ADVANCE_TASK)
def pls_advance_search_tick(**kwargs) -> Dict[str, Any]:
    return _sync_tick(ADVANCE)


@shared_task(name=SMART_TASK)
def pls_smart_search_tick(**kwargs) -> Dict[str, Any]:
    return _sync_tick(SMART)
