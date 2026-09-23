"""
Automatic re-verification of PakistanLawSite login slots, so a slot the site bounced comes back
into rotation on its own when the session behind it is in fact still alive.

The site ends a busy session by redirecting to its public login page; the connector then marks the
slot NEEDS_HUMAN_LOGIN and, when both slots are down, pauses the source. Whether the site ended the
login or only throttled the account is not known at that moment. This Beat task finds out:

1. LOGIN_RECOVERY_COOLDOWN_MINUTES after the loss it opens a headless browser with the slot's stored
   session (the human's login, nothing else) and loads the authenticated search page.
2. If the page renders, the slot is ACTIVE again and a paused source resumes at once.
3. If the site still bounces to its login page, the attempt is recorded and the next one waits
   longer (15, 30, 60 minutes, then hourly). No credentials are ever submitted by this task: a dead
   login is re-established by the human, through the dashboard, as the contract prescribes.

An explicit block (HTTP 403/429/451 or a block page) halts the source as it always has. Attempts
are recorded in the source's config_json under `slot_recovery_<n>` (never cookie values).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from celery import shared_task
from sqlalchemy import select

from scraper.auth.session_manager import (
    SessionLock,
    SessionLockHeld,
    SessionManager,
    merge_source_config,
    playwright_browser_factory,
    safe_url_for_record,
)
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.models import BrowserSessionSlot, ScraperSource
from scraper.notify import notify

logger = logging.getLogger(__name__)

BACKOFF_MINUTES = (15, 30, 60)


def recovery_key(slot_number: int) -> str:
    return f"slot_recovery_{int(slot_number)}"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def looks_authenticated(page) -> bool:
    """The search page rendered for this session: the citation grid is there, or the page offers a
    logout, and the URL is not the public login surface."""
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline

    url_low = (page.url or "").lower()
    if any(p in url_low for p in ("/login/mainpage", "/login/login", "/login/index")):
        return False
    if PakistanLawSitePipeline._is_citation_grid_surface(page):
        return True
    low = (page.html or "").lower()
    return "logout" in low or "log off" in low or "sign out" in low


async def verify_stored_session(manager: SessionManager, slot: BrowserSessionSlot, browser_factory: Callable) -> Dict[str, Any]:
    """Open the slot's stored session and load the authenticated search page. Returns
    {"alive": bool, "verdict": kind, "landed": url}; site answers never raise."""
    state = manager.load_storage_state(slot)
    browser = await browser_factory(state, slot.slot_number)
    try:
        page = await browser.goto(settings.PLS_SEARCH_URL)
    except Exception as exc:  # navigation error or disconnect: not verified, retry later
        return {"alive": False, "verdict": "navigation_error", "detail": str(exc)[:300], "landed": None}
    finally:
        try:
            await browser.close()
        except Exception:
            pass
    verdict = page.classify()
    landed = safe_url_for_record(page.url) if page.url else None
    if verdict.kind == "block":
        return {"alive": False, "verdict": "block", "detail": verdict.detail, "landed": landed}
    if verdict.kind == "ok" and looks_authenticated(page):
        return {"alive": True, "verdict": "ok", "detail": verdict.detail, "landed": landed}
    return {"alive": False, "verdict": verdict.kind, "detail": verdict.detail, "landed": landed}


async def recover_slot(
    db,
    manager: SessionManager,
    slot: BrowserSessionSlot,
    *,
    now: Optional[datetime] = None,
    browser_factory: Callable = playwright_browser_factory,
    redis_client=None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    source = manager.source
    key = recovery_key(slot.slot_number)
    record = dict((source.config_json or {}).get(key) or {})
    if slot.state == "ACTIVE":
        if record:
            await merge_source_config(db, source, {key: {}})
        return {"skipped": "ACTIVE"}
    if slot.state != "NEEDS_HUMAN_LOGIN" or not slot.storage_state_encrypted:
        return {"skipped": slot.state if slot.state != "NEEDS_HUMAN_LOGIN" else "no stored session"}
    if not settings.LOGIN_AUTO_RECOVER:
        return {"skipped": "LOGIN_AUTO_RECOVER off"}

    if not record:
        cooldown = timedelta(minutes=int(settings.LOGIN_RECOVERY_COOLDOWN_MINUTES))
        record = {"attempts": 0, "lost_at": _iso(now), "next_attempt_at": _iso(now + cooldown), "last_outcome": "scheduled"}
        await merge_source_config(db, source, {key: record})
        return {"scheduled": record["next_attempt_at"]}
    next_at = _parse(record.get("next_attempt_at"))
    if next_at is not None and now < next_at:
        return {"waiting_until": record.get("next_attempt_at")}

    slot_lock = SessionLock(f"{source.source_name}:slot{slot.slot_number}", redis_client, max_holders=1)
    try:
        await slot_lock.acquire()
    except SessionLockHeld:
        return {"skipped": "slot_in_use"}
    try:
        attempts = int(record.get("attempts", 0) or 0) + 1
        check = await verify_stored_session(manager, slot, browser_factory)
        outcome: Dict[str, Any] = {"attempt": attempts, "verify": check}
        if check["verdict"] == "block":
            await manager.halt_source(f"explicit block while re-verifying slot {slot.slot_number}: {check.get('detail')}", slot_number=slot.slot_number)
            await merge_source_config(db, source, {key: {**record, "attempts": attempts, "last_attempt_at": _iso(now), "last_outcome": "halted"}})
            return {**outcome, "halted": True}
        if check["alive"]:
            await manager.reactivate_slot(slot.slot_number, f"stored session verified after cool-down (attempt {attempts})", by="recovery")
            await merge_source_config(db, source, {key: {}})
            return {**outcome, "recovered": "verified"}
        reason = f"stored session still bounced by the site ({check.get('verdict')}; landed on {check.get('landed')})"
        next_attempt = now + timedelta(minutes=BACKOFF_MINUTES[min(attempts, len(BACKOFF_MINUTES)) - 1])
        await merge_source_config(
            db,
            source,
            {key: {**record, "attempts": attempts, "last_attempt_at": _iso(now), "next_attempt_at": _iso(next_attempt), "last_outcome": reason[:300]}},
        )
        if attempts == 1 or attempts % 6 == 0:
            await notify(
                db,
                level="warning",
                code="SLOT_RECOVERY_FAILED",
                message=f"slot {slot.slot_number}: {reason}; a human login from the dashboard restores it now, the next automatic check is at {next_attempt:%H:%M} UTC (attempt {attempts})",
                source_name=source.source_name,
            )
        return {**outcome, "failed": reason, "next_attempt_at": _iso(next_attempt)}
    finally:
        await slot_lock.release()


async def recover_login_slots_async(
    *,
    now: Optional[datetime] = None,
    browser_factory: Callable = playwright_browser_factory,
    redis_client=None,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    if not settings.login_scraping_effective:
        return {"skipped": "login scraping not permitted here"}
    async with SessionLocal() as db:
        sources = (
            await db.execute(
                select(ScraperSource).where(
                    ScraperSource.access_method == "login_session",
                    ScraperSource.is_active.is_(True),
                    ScraperSource.state.notin_(["HALTED", "DISABLED"]),
                )
            )
        ).scalars().all()
        for source in sources:
            manager = SessionManager(db, source)
            for slot in await manager.slots():
                try:
                    outcome = await recover_slot(db, manager, slot, now=now, browser_factory=browser_factory, redis_client=redis_client)
                except Exception as exc:  # one slot's failure must not stop the other's check
                    logger.warning("login recovery for %s slot %s failed: %s", source.source_name, slot.slot_number, exc)
                    outcome = {"error": str(exc)[:300]}
                results[f"{source.source_name}:{slot.slot_number}"] = outcome
                await db.commit()
                if source.state == "HALTED":
                    break
    return results


@shared_task(name="scraper.tasks.login_recovery.recover_login_slots")
def recover_login_slots() -> Dict[str, Any]:
    return run_async(recover_login_slots_async())
