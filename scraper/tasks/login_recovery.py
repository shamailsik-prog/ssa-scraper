"""
Automatic re-verification of PakistanLawSite login slots and, when a slot is dead, sign-in with
the credentials the operator saved for it (operator decision of 24 September 2026, after the
working specification's human-only login had been deployed and the operator asked for the saved
passwords and the automatic login back, with the box below the password ticked).

The site ends a busy session by redirecting to its public login page; the connector then marks the
slot NEEDS_HUMAN_LOGIN and, when both slots are down, pauses the source. Whether the site ended the
login or only throttled the account is not known at that moment. This Beat task finds out:

1. LOGIN_RECOVERY_COOLDOWN_MINUTES after the loss it opens a headless browser with the slot's stored
   session (the human's login, nothing else) and loads the authenticated search page.
2. If the page renders, the slot is ACTIVE again and a paused source resumes at once.
3. If the site still bounces to its login page, and the operator saved username/password for the
   slot (dashboard → Human login, encrypted at rest), the same server-side browser used for the
   streamed human login opens the sign-in page, fills and submits the form, and the resulting
   session is stored in the slot. The operator authorised this unattended sign-in expressly
   (23 September 2026) so the harvest runs around the clock on the firm's two logins.
4. A verification page (CAPTCHA, OTP, "verify you are human") is never solved: the slot waits for a
   human and the next automatic attempt is two hours away. Without saved credentials the attempt
   backs off (15, 30, 60 minutes, then hourly) and a human login from the dashboard restores it.

An explicit block (HTTP 403/429/451 or a block page) halts the source as it always has. Attempts
are recorded in the source's config_json under `slot_recovery_<n>` (never credentials or cookie
values); credentials are never logged or returned by any API.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from celery import shared_task
from sqlalchemy import select

from scraper.auth.browser_login import LoginSessionRegistry
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
SOURCE_RESUME_COOLDOWN_MINUTES = 15


async def resume_paused_source(db, manager: SessionManager, *, now: Optional[datetime] = None) -> Optional[str]:
    """A source paused for a continuity reason ("no valid slot", "unreachable after reconnect") stays
    paused even after its slots are ACTIVE again, because only a slot state change resumed it. When a
    slot is ACTIVE and the pause is older than the cool-down, resume the source. A pause set by an
    admin is left alone."""
    now = now or datetime.now(timezone.utc)
    source = manager.source
    if source.state != "PAUSED":
        return None
    if manager.is_admin_paused():
        return None
    if not any(s.state == "ACTIVE" for s in await manager.slots()):
        return None
    changed = source.state_changed_at
    if changed is not None and changed.tzinfo is None:
        changed = changed.replace(tzinfo=timezone.utc)
    if changed is not None and now - changed < timedelta(minutes=SOURCE_RESUME_COOLDOWN_MINUTES):
        return None
    was = source.state_reason or "no reason recorded"
    await manager._resume_source_if_paused(respect_admin=True)
    await db.flush()
    await notify(
        db,
        level="info",
        code="SOURCE_RESUMED",
        message=f"resumed: a slot is ACTIVE again (was paused: {was})",
        source_name=source.source_name,
    )
    return "resumed"
VERIFICATION_BACKOFF_MINUTES = 120


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
    from scraper.extractors.deterministic import introspect_search_form

    url_low = (page.url or "").lower()
    if any(p in url_low for p in ("/login/mainpage", "/login/login", "/login/index")):
        return False
    probe = introspect_search_form(page.html or "")
    if probe.get("surface") == "login_required":
        return False
    if probe.get("surface") == "query_form":
        return True
    low = (page.html or "").lower()
    if "archivedpatientgrid" in low or "logout" in low or "log off" in low or "sign out" in low:
        return True
    return False


async def verify_stored_session(manager: SessionManager, slot: BrowserSessionSlot, browser_factory: Callable) -> Dict[str, Any]:
    """Open the slot's stored session and load the authenticated search page. Returns
    {"alive": bool, "verdict": kind, "landed": url}; site answers never raise."""
    state = manager.load_storage_state(slot)
    browser = await browser_factory(state, slot.slot_number)
    renewed_state = None
    try:
        page = await browser.goto(settings.PLS_SEARCH_URL)
        exporter = getattr(browser, "export_storage_state", None)
        if callable(exporter):
            try:
                renewed_state = await exporter()  # the cookies the site renewed on this load
            except Exception as exc:
                logger.debug("verify: storage state export skipped: %s", exc)
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
        return {"alive": True, "verdict": "ok", "detail": verdict.detail, "landed": landed, "renewed_state": renewed_state}
    return {"alive": False, "verdict": verdict.kind, "detail": verdict.detail, "landed": landed}


def _sign_in_deadline_seconds() -> float:
    # Three page loads at the Playwright timeout plus the submit wait, then the attempt is abandoned.
    return 3 * float(settings.PLAYWRIGHT_TIMEOUT_MS) / 1000.0 + float(settings.LOGIN_RECOVERY_SUBMIT_WAIT_SECONDS) + 30.0


async def sign_in_with_saved_credentials(
    manager: SessionManager,
    slot: BrowserSessionSlot,
    credentials: Dict[str, str],
    registry_factory: Callable[[], LoginSessionRegistry] = LoginSessionRegistry,
) -> Dict[str, Any]:
    """Sign in with the operator's saved credentials in the server-side browser and store the
    resulting session in the slot (the same path the dashboard's *Human login* uses, completed by
    the service instead of a person). Returns {"stored": bool, "verdict": kind, "detail": str}.
    A verification page is reported as verdict "verification", never solved. Credentials are never
    logged or included in the result. The whole attempt is bounded: a browser that stops answering
    can never hold the recovery task."""
    source_name = manager.source.source_name
    registry = registry_factory()
    try:
        return await asyncio.wait_for(_sign_in(registry, manager, slot, credentials), timeout=_sign_in_deadline_seconds())
    finally:
        await registry.cancel(source_name)


async def _sign_in(registry, manager: SessionManager, slot: BrowserSessionSlot, credentials: Dict[str, str]) -> Dict[str, Any]:
    source_name = manager.source.source_name
    sess = await registry.start(
        source_name,
        slot.slot_number,
        settings.PLS_LOGIN_URL if source_name == "PakistanLawSite" else manager.source.source_url,
        started_by="auto-recovery",
        saved_credentials=credentials,
        auto_complete=True,
    )
    autofill = dict(sess.last_autofill or {})
    if not autofill.get("submitted"):
        state = await sess.is_authenticated()
        if state.get("verdict") == "block":
            return {"stored": False, "verdict": "block", "detail": state.get("detail"), "landed": safe_url_for_record(state.get("url") or "")}
        return {
            "stored": False,
            "verdict": state.get("verdict", "unknown"),
            "detail": "sign-in form not recognised; nothing submitted",
            "landed": safe_url_for_record(state.get("url") or ""),
        }
    settle = getattr(sess, "settle", None)
    if callable(settle):
        await settle()  # the submitted form's navigation reaches the page the site answered with
    await asyncio.sleep(max(0.0, float(settings.LOGIN_RECOVERY_SUBMIT_WAIT_SECONDS)))
    login_error = getattr(sess, "login_error", None)
    refused = await login_error() if callable(login_error) else None
    check = await sess.is_authenticated_for(settings.PLS_SEARCH_URL)
    landed = safe_url_for_record(check.get("url") or "")
    if check.get("verdict") in ("verification", "block"):
        return {"stored": False, "verdict": check.get("verdict"), "detail": check.get("detail"), "landed": landed}
    if not check.get("authenticated"):
        detail = f"site refused the sign-in: {refused}" if refused else check.get("detail")
        return {"stored": False, "verdict": check.get("verdict", "login"), "detail": detail, "landed": landed}
    result = await registry.complete(source_name, manager)
    return {"stored": bool(result.get("stored")), "verdict": result.get("verdict", "ok"), "detail": result.get("detail"), "landed": landed}


async def _wait_for_human(db, source, slot, key, record, attempts, now, what: str, outcome: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A verification page is never solved by code: record the attempt, wait two hours before the
    next automatic one, and ask for a human login."""
    next_attempt = now + timedelta(minutes=VERIFICATION_BACKOFF_MINUTES)
    await merge_source_config(
    db,
    source,
    {key: {**record, "attempts": attempts, "last_attempt_at": _iso(now), "next_attempt_at": _iso(next_attempt), "last_outcome": what}},
    )
    await notify(
    db,
    level="warning",
    code="NEEDS_HUMAN_LOGIN",
    message=f"slot {slot.slot_number}: {what}; it is never solved by code. Log in from the dashboard (next automatic attempt {next_attempt:%H:%M} UTC).",
    source_name=source.source_name,
    )
    return {**(outcome or {"attempt": attempts}), "verification": True, "next_attempt_at": _iso(next_attempt)}


async def recover_slot(
    db,
    manager: SessionManager,
    slot: BrowserSessionSlot,
    *,
    now: Optional[datetime] = None,
    browser_factory: Callable = playwright_browser_factory,
    registry_factory: Callable[[], LoginSessionRegistry] = LoginSessionRegistry,
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
    if slot.state not in ("NEEDS_HUMAN_LOGIN", "EMPTY"):
        return {"skipped": slot.state}
    if not settings.LOGIN_AUTO_RECOVER:
        return {"skipped": "LOGIN_AUTO_RECOVER off"}
    credentials = manager.load_login_credentials(slot)
    can_verify = slot.state == "NEEDS_HUMAN_LOGIN" and bool(slot.storage_state_encrypted)
    if not can_verify and not credentials:
        return {"skipped": "EMPTY without saved credentials" if slot.state == "EMPTY" else "no stored session and no saved credentials"}

    if not record:
        cooldown = timedelta(minutes=int(settings.LOGIN_RECOVERY_COOLDOWN_MINUTES)) if slot.state == "NEEDS_HUMAN_LOGIN" else timedelta(0)
        record = {"attempts": 0, "lost_at": _iso(now), "next_attempt_at": _iso(now + cooldown), "last_outcome": "scheduled"}
        await merge_source_config(db, source, {key: record})
        return {"scheduled": record["next_attempt_at"]}
    next_at = _parse(record.get("next_attempt_at"))
    if next_at is not None and now < next_at:
        return {"waiting_until": record.get("next_attempt_at")}

    # The recovery task runs on the single login-session worker, so it never runs beside a harvest
    # job; the source lock is still taken so a stale lock or a second worker is respected.
    source_lock = SessionLock(source.source_name, redis_client)
    try:
        await source_lock.acquire()
    except SessionLockHeld:
        await source_lock.release()  # closes the client the lock opened for itself
        return {"skipped": "login_session_lock_held"}
    try:
        attempts = int(record.get("attempts", 0) or 0) + 1
        outcome: Dict[str, Any] = {"attempt": attempts}
        reason = ""

        # 1. The stored session may still be alive once the account has cooled down.
        if can_verify:
            check = await verify_stored_session(manager, slot, browser_factory)
            outcome["verify"] = check
            if check["verdict"] == "block":
                await manager.halt_source(f"explicit block while re-verifying slot {slot.slot_number}: {check.get('detail')}", slot_number=slot.slot_number)
                await merge_source_config(db, source, {key: {**record, "attempts": attempts, "last_attempt_at": _iso(now), "last_outcome": "halted"}})
                return {**outcome, "halted": True}
            if check["alive"]:
                renewed = check.pop("renewed_state", None)
                await manager.reactivate_slot(slot.slot_number, f"stored session verified after cool-down (attempt {attempts})", by="recovery")
                if renewed:
                    # Keep the cookies the site renewed on the verifying load, so the next job does
                    # not open with the ones that were already stale (the slot is ACTIVE again, so
                    # the compare-and-update applies).
                    await manager.refresh_storage_state(slot.slot_number, renewed, expected_hash=slot.storage_state_hash)
                await merge_source_config(db, source, {key: {}})
                return {**outcome, "recovered": "verified"}
            if check["verdict"] == "verification":
                # The site wants a human on this session: no credentials are submitted behind it.
                return await _wait_for_human(db, source, slot, key, record, attempts, now, "verification page when re-opening the stored session")
            reason = f"stored session still bounced by the site ({check.get('verdict')}; landed on {check.get('landed')})"

        # 2. Sign in again with the credentials the operator saved for this slot.
        if credentials:
            try:
                relogin = await sign_in_with_saved_credentials(manager, slot, credentials, registry_factory)
            except Exception as exc:  # browser launch / navigation failure: count the attempt, back off, never loop every 5 minutes
                relogin = {"stored": False, "verdict": "error", "detail": str(exc)[:300], "landed": None}
                logger.warning("sign-in with saved credentials failed for %s slot %s: %s", source.source_name, slot.slot_number, exc)
            outcome["sign_in"] = relogin
            if relogin.get("verdict") == "block":
                # An explicit block on the sign-in surface is the one answer that must never be retried.
                await manager.halt_source(f"explicit block while signing in on slot {slot.slot_number}: {relogin.get('detail')}", slot_number=slot.slot_number)
                await merge_source_config(db, source, {key: {**record, "attempts": attempts, "last_attempt_at": _iso(now), "last_outcome": "halted at sign-in"}})
                return {**outcome, "halted": True}
            if relogin.get("stored"):
                await merge_source_config(db, source, {key: {}})
                await notify(
                    db,
                    level="info",
                    code="SLOT_RECOVERED",
                    message=f"slot {slot.slot_number}: signed in again with the saved credentials (attempt {attempts})",
                    source_name=source.source_name,
                )
                return {**outcome, "recovered": "signed_in"}
            if relogin.get("verdict") == "verification":
                return await _wait_for_human(db, source, slot, key, record, attempts, now, "verification page at sign-in", outcome)
            reason = f"sign-in with the saved credentials did not authenticate ({relogin.get('verdict')}: {relogin.get('detail')}; landed on {relogin.get('landed')})"
        else:
            reason = f"{reason}; no saved credentials for this slot" if reason else "no saved credentials for this slot"

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
                message=f"slot {slot.slot_number}: {reason}; a human login from the dashboard restores it now, the next automatic attempt is at {next_attempt:%H:%M} UTC (attempt {attempts})",
                source_name=source.source_name,
            )
        return {**outcome, "failed": reason, "next_attempt_at": _iso(next_attempt)}
    finally:
        await source_lock.release()


async def recover_login_slots_async(
    *,
    now: Optional[datetime] = None,
    browser_factory: Callable = playwright_browser_factory,
    registry_factory: Callable[[], LoginSessionRegistry] = LoginSessionRegistry,
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
                    outcome = await recover_slot(
                        db, manager, slot, now=now, browser_factory=browser_factory, registry_factory=registry_factory, redis_client=redis_client
                    )
                except Exception as exc:  # one slot's failure must not stop the other's check
                    logger.warning("login recovery for %s slot %s failed: %s", source.source_name, slot.slot_number, exc)
                    outcome = {"error": str(exc)[:300]}
                results[f"{source.source_name}:{slot.slot_number}"] = outcome
                await db.commit()
                if source.state == "HALTED":
                    break
            if source.state == "PAUSED":
                try:
                    resumed = await resume_paused_source(db, manager, now=now)
                    if resumed:
                        results[f"{source.source_name}:source"] = resumed
                        await db.commit()
                except Exception as exc:
                    logger.warning("resume of paused source %s failed: %s", source.source_name, exc)
    return results


@shared_task(name="scraper.tasks.login_recovery.recover_login_slots")
def recover_login_slots() -> Dict[str, Any]:
    return run_async(recover_login_slots_async())
