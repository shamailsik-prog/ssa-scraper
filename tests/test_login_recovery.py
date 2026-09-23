"""Automatic re-verification of bounced login slots (F18) and one-browser-per-login guards (F19)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.auth.session_manager import SessionLock, SessionManager
from scraper.config import settings
from scraper.models import BrowserSessionSlot, Notification, ScraperSource
from scraper.tasks import login_recovery
from scraper.tasks.login_recovery import recover_slot, recovery_key
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
from tests.fixtures import BrowserScript
from tests.test_auth_playwright import _activate, _nosleep

GRID_HTML = '<html><body><a href="/logout">Logout</a><table id="archivedpatientGrid"><tr><td>PLD 2024 SC 1</td></tr></table></body></html>'
MAINPAGE_HTML = '<html><body><form id="mainLoginForm" action="/Login/Login"><input name="Login.UserName"><input type="password" name="Login.Password"></form></body></html>'
MAINPAGE_URL = "https://www.pakistanlawsite.com/Login/MainPage?ReturnUrl=%2FLogin%2FCitationSearch"


async def _bounce_slot(db, source, slot_number=1):
    mgr = await _activate(db, source, slots=(slot_number,))
    await mgr.mark_needs_human_login(slot_number, "LoginRequired: login surface URL (landed on https://www.pakistanlawsite.com/Login/MainPage)")
    await db.commit()
    return mgr


async def _slot(db, n):
    return (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite", BrowserSessionSlot.slot_number == n))).scalars().first()


async def test_bounced_slot_is_scheduled_then_verified_and_reactivated(db, login_source, monkeypatch):
    """First sighting only schedules the check after the cool-down; once due, a stored session that
    still renders the search page puts the slot back and resumes the paused source."""
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 15)
    mgr = await _bounce_slot(db, login_source)
    assert login_source.state == "PAUSED"
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), GRID_HTML)

    first = await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory())
    assert "scheduled" in first
    assert (login_source.config_json or {})[recovery_key(1)]["attempts"] == 0
    assert sc.log == []  # nothing opened before the cool-down

    early = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(minutes=5), browser_factory=sc.factory())
    assert "waiting_until" in early and sc.log == []

    done = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(minutes=16), browser_factory=sc.factory())
    await db.commit()
    assert done.get("recovered") == "verified"
    slot = await _slot(db, 1)
    assert slot.state == "ACTIVE" and "verified after cool-down" in slot.state_reason
    assert login_source.state == "ACTIVE" and login_source.next_scrape_at is not None
    assert (login_source.config_json or {}).get(recovery_key(1)) == {}
    codes = [n.code for n in (await db.execute(select(Notification).where(Notification.source_name == "PakistanLawSite"))).scalars().all()]
    assert "SLOT_ACTIVE" in codes


async def test_bounced_slot_still_dead_backs_off_and_never_submits_credentials(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
    slot = await _slot(db, 1)
    slot.login_username_encrypted = settings.encrypt_value("user")
    slot.login_password_encrypted = settings.encrypt_value("secret-password")
    await db.commit()
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), MAINPAGE_HTML, url=MAINPAGE_URL)

    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory())
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(seconds=1), browser_factory=sc.factory())
    await db.commit()
    assert "failed" in result and result["verify"]["alive"] is False
    assert result["verify"]["landed"] == "https://www.pakistanlawsite.com/Login/MainPage"  # query string never recorded
    record = (login_source.config_json or {})[recovery_key(1)]
    assert record["attempts"] == 1
    assert datetime.fromisoformat(record["next_attempt_at"]) == t0 + timedelta(seconds=1) + timedelta(minutes=15)
    assert (await _slot(db, 1)).state == "NEEDS_HUMAN_LOGIN"
    # Only a goto of the search page with the stored session; no login page, no form submission.
    assert [k for k, _ in sc.log] == [("goto", settings.PLS_SEARCH_URL)]
    notes = (await db.execute(select(Notification).where(Notification.code == "SLOT_RECOVERY_FAILED"))).scalars().all()
    assert len(notes) == 1 and "secret-password" not in notes[0].message and "human login" in notes[0].message

    # Second failure backs off 30 minutes and does not notify again.
    later = datetime.fromisoformat(record["next_attempt_at"]) + timedelta(seconds=1)
    result2 = await recover_slot(db, mgr, await _slot(db, 1), now=later, browser_factory=sc.factory())
    await db.commit()
    record2 = (login_source.config_json or {})[recovery_key(1)]
    assert record2["attempts"] == 2 and datetime.fromisoformat(record2["next_attempt_at"]) == later + timedelta(minutes=30)
    notes = (await db.execute(select(Notification).where(Notification.code == "SLOT_RECOVERY_FAILED"))).scalars().all()
    assert len(notes) == 1


async def test_explicit_block_during_reverify_halts_source(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), "<html>forbidden</html>", status=403)
    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory())
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(seconds=1), browser_factory=sc.factory())
    await db.commit()
    assert result.get("halted") is True
    assert login_source.state == "HALTED" and (await _slot(db, 1)).state == "HALTED"


async def test_recovery_skips_active_empty_and_disabled_slots(db, login_source, monkeypatch):
    mgr = await _activate(db, login_source, slots=(1,))
    sc = BrowserScript()
    assert (await recover_slot(db, mgr, await _slot(db, 1), browser_factory=sc.factory())) == {"skipped": "ACTIVE"}
    assert (await recover_slot(db, mgr, await _slot(db, 2), browser_factory=sc.factory())) == {"skipped": "EMPTY"}
    monkeypatch.setattr(settings, "LOGIN_AUTO_RECOVER", False)
    await mgr.mark_needs_human_login(1, "LoginRequired: test")
    await db.commit()
    assert (await recover_slot(db, mgr, await _slot(db, 1), browser_factory=sc.factory())) == {"skipped": "LOGIN_AUTO_RECOVER off"}
    assert sc.log == []


async def test_recovery_task_walks_both_slots_and_leaves_alternate_running(db, login_source, monkeypatch):
    """Slot 1 bounced, slot 2 healthy: the task re-verifies slot 1 only and never touches slot 2."""
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    monkeypatch.setattr(settings, "ALLOW_LOGIN_SCRAPING", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "chambers")
    mgr = await _activate(db, login_source, slots=(1, 2))
    await mgr.mark_needs_human_login(1, "LoginRequired: login surface URL")
    await db.commit()
    assert login_source.state == "ACTIVE"  # slot 2 still serves
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), GRID_HTML)
    t0 = datetime.now(timezone.utc)
    first = await login_recovery.recover_login_slots_async(now=t0, browser_factory=sc.factory())
    assert "scheduled" in first["PakistanLawSite:1"] and first["PakistanLawSite:2"] == {"skipped": "ACTIVE"}
    second = await login_recovery.recover_login_slots_async(now=t0 + timedelta(seconds=1), browser_factory=sc.factory())
    assert second["PakistanLawSite:1"].get("recovered") == "verified"
    assert [slot for _, slot in sc.log] == [1]
    async with __import__("scraper.database", fromlist=["SessionLocal"]).SessionLocal() as fresh:
        rows = (await fresh.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite").order_by(BrowserSessionSlot.slot_number))).scalars().all()
        assert [r.state for r in rows] == ["ACTIVE", "ACTIVE"]


async def test_pipeline_refuses_a_slot_another_worker_holds(db, login_source):
    """One browser per login: a second job that lands on a slot already locked skips instead of
    opening a second browser on the same cookies."""
    await _activate(db, login_source, slots=(1,))
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite", "corpus:login_session_lock:PakistanLawSite:holders", "corpus:login_session_lock:PakistanLawSite:slot1")
    holder = SessionLock("PakistanLawSite:slot1", r, max_holders=1)
    await holder.acquire()
    try:
        sc = BrowserScript()
        pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
        stats = await pipeline.run(max_queries=1, max_probes_per_volume=1)
        assert stats.get("skipped") == "slot_in_use" and stats.get("slot") == 1
        assert sc.log == []
    finally:
        await holder.release()
        await r.aclose()


async def test_merge_source_config_keeps_other_keys(db, login_source):
    from scraper.auth.session_manager import merge_source_config

    await merge_source_config(db, login_source, {"citation_grid_cursor_shard_0": {"row_offset": 10}})
    await merge_source_config(db, login_source, {"citation_grid_cursor_shard_1": {"row_offset": 20}})
    await db.commit()
    async with __import__("scraper.database", fromlist=["SessionLocal"]).SessionLocal() as fresh:
        row = (await fresh.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
        assert row.config_json["citation_grid_cursor_shard_0"] == {"row_offset": 10}
        assert row.config_json["citation_grid_cursor_shard_1"] == {"row_offset": 20}
    assert login_source.config_json["citation_grid_cursor_shard_0"] == {"row_offset": 10}
