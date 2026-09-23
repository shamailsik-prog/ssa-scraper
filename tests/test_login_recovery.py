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


async def test_bounced_slot_without_saved_credentials_backs_off(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
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
    assert len(notes) == 1 and "no saved credentials" in notes[0].message and "human login" in notes[0].message

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
    assert (await recover_slot(db, mgr, await _slot(db, 2), browser_factory=sc.factory())) == {"skipped": "EMPTY without saved credentials"}
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


class FakeLoginSession:
    """Stands in for browser_login.LoginSession: records what the recovery task asked of it."""

    def __init__(self, registry, slot_number, saved_credentials, auto_complete):
        self.registry = registry
        self.slot_number = slot_number
        self.started_by = "auto-recovery"
        self.status = "awaiting_human"
        self.last_autofill = {"applied": bool(saved_credentials), "submitted": bool(saved_credentials and auto_complete)}
        self.registry.log.append(("start", slot_number, dict(saved_credentials or {}), auto_complete))

    async def is_authenticated(self):
        return {"authenticated": False, "verdict": "login", "detail": "form", "url": "https://www.pakistanlawsite.com/Login/MainPage"}

    async def is_authenticated_for(self, expected_url):
        self.registry.log.append(("check", expected_url))
        return dict(self.registry.outcome)

    async def export_storage_state(self):
        return {"cookies": [{"name": "ASP.NET_SessionId", "value": "fresh-session", "domain": "www.pakistanlawsite.com", "path": "/", "expires": -1}], "origins": []}

    async def close(self):
        self.status = "closed"


class FakeRegistry:
    """Stands in for LoginSessionRegistry with a scripted authentication outcome."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.log = []
        self._sessions = {}

    async def start(self, source_name, slot_number, login_url, started_by="operator", viewport=None, saved_credentials=None, auto_complete=False):
        assert started_by == "auto-recovery"
        sess = FakeLoginSession(self, slot_number, saved_credentials, auto_complete)
        self._sessions[source_name] = sess
        return sess

    async def complete(self, source_name, manager):
        sess = self._sessions[source_name]
        state = await sess.export_storage_state()
        await manager.save_storage_state(sess.slot_number, state, by=sess.started_by)
        await sess.close()
        self._sessions.pop(source_name, None)
        self.log.append(("complete", sess.slot_number))
        return {"stored": True, "slot": sess.slot_number, "verdict": "ok"}

    async def cancel(self, source_name):
        sess = self._sessions.pop(source_name, None)
        if sess is not None:
            await sess.close()
        self.log.append(("cancel",))
        return sess is not None


AUTHENTICATED = {"authenticated": True, "verdict": "ok", "detail": "", "url": "https://www.pakistanlawsite.com/Login/CitationSearch"}
VERIFICATION = {"authenticated": False, "verdict": "verification", "detail": "verify you are human", "url": "https://www.pakistanlawsite.com/Login/Login?x=1"}
BLOCKED = {"authenticated": False, "verdict": "block", "detail": "HTTP 403", "url": "https://www.pakistanlawsite.com/Login/Login"}


async def _save_credentials(db, n, username="firm-user", password="firm-secret"):
    slot = await _slot(db, n)
    slot.login_username_encrypted = settings.encrypt_value(username)
    slot.login_password_encrypted = settings.encrypt_value(password)
    await db.commit()


async def test_dead_session_is_signed_in_again_with_saved_credentials(db, login_source, monkeypatch):
    """Cool-down re-verify fails (site really ended the login) → the saved credentials sign in
    again, the new session is stored, the slot is ACTIVE and the paused source resumes."""
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
    await _save_credentials(db, 1)
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), MAINPAGE_HTML, url=MAINPAGE_URL)
    reg = FakeRegistry(AUTHENTICATED)
    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory(), registry_factory=lambda: reg)
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(seconds=1), browser_factory=sc.factory(), registry_factory=lambda: reg)
    await db.commit()
    assert result.get("recovered") == "signed_in" and result["verify"]["alive"] is False
    assert reg.log[0] == ("start", 1, {"username": "firm-user", "password": "firm-secret"}, True)
    assert ("complete", 1) in reg.log
    slot = await _slot(db, 1)
    assert slot.state == "ACTIVE" and slot.logged_in_by == "auto-recovery"
    state = __import__("json").loads(settings.decrypt_value(slot.storage_state_encrypted))
    assert state["cookies"][0]["value"] == "fresh-session"
    assert login_source.state == "ACTIVE"
    assert (login_source.config_json or {}).get(recovery_key(1)) == {}
    notes = (await db.execute(select(Notification).where(Notification.source_name == "PakistanLawSite"))).scalars().all()
    assert any(n.code == "SLOT_RECOVERED" for n in notes)
    assert all("firm-secret" not in n.message and "firm-user" not in n.message for n in notes)


async def test_verification_page_at_sign_in_waits_for_a_human(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
    await _save_credentials(db, 1)
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), MAINPAGE_HTML, url=MAINPAGE_URL)
    reg = FakeRegistry(VERIFICATION)
    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory(), registry_factory=lambda: reg)
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(seconds=1), browser_factory=sc.factory(), registry_factory=lambda: reg)
    await db.commit()
    assert result.get("verification") is True and ("complete", 1) not in reg.log and ("cancel",) in reg.log
    assert (await _slot(db, 1)).state == "NEEDS_HUMAN_LOGIN"
    record = (login_source.config_json or {})[recovery_key(1)]
    assert datetime.fromisoformat(record["next_attempt_at"]) == t0 + timedelta(seconds=1) + timedelta(minutes=120)
    notes = (await db.execute(select(Notification).where(Notification.code == "NEEDS_HUMAN_LOGIN"))).scalars().all()
    assert any("verification page" in n.message and "never solved" in n.message for n in notes)


async def test_empty_slot_with_saved_credentials_is_signed_in_without_cool_down(db, login_source, monkeypatch):
    """Slot 2 was never logged in but the operator saved its credentials: it is brought up at once,
    so both logins run."""
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 15)
    mgr = await _activate(db, login_source, slots=(1,))
    await _save_credentials(db, 2, username="second-login", password="second-secret")
    t0 = datetime.now(timezone.utc)
    reg = FakeRegistry(AUTHENTICATED)
    sc = BrowserScript()
    first = await recover_slot(db, mgr, await _slot(db, 2), now=t0, browser_factory=sc.factory(), registry_factory=lambda: reg)
    assert first["scheduled"] == (t0).isoformat()  # no cool-down for an EMPTY slot
    result = await recover_slot(db, mgr, await _slot(db, 2), now=t0 + timedelta(seconds=1), browser_factory=sc.factory(), registry_factory=lambda: reg)
    await db.commit()
    assert result.get("recovered") == "signed_in" and "verify" not in result and sc.log == []
    assert (await _slot(db, 2)).state == "ACTIVE"


async def test_unattended_sign_in_end_to_end_with_real_browser(db, login_source, fixture_server, monkeypatch):
    """Real Playwright: the saved credentials are typed into the fixture's login form and submitted,
    the search page renders, and the resulting cookies are stored in the slot."""
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_SUBMIT_WAIT_SECONDS", 0.5)
    fixture_server.add(
        "/",
        "<html><body><form id='mainLoginForm' action='/Login/Login' onsubmit=\"document.cookie='submitted='+encodeURIComponent(this['Login.UserName'].value+'/'+this['Login.Password'].value)+'; path=/'\">"
        "<input name='Login.UserName'><input type='password' name='Login.Password'><button type='submit'>Log in</button></form></body></html>",
    )
    fixture_server.add("/Login/Login", "<html><body>signed in</body></html>")
    # The authenticated page sets the session cookie the slot must end up holding.
    fixture_server.add("/Login/CitationSearch", GRID_HTML.replace("<body>", "<body><script>document.cookie='ASP.NET_SessionId=e2e; path=/';</script>"))
    monkeypatch.setattr(settings, "PLS_LOGIN_URL", fixture_server.url("/"))
    monkeypatch.setattr(settings, "PLS_SEARCH_URL", fixture_server.url("/Login/CitationSearch"))
    mgr = await _bounce_slot(db, login_source)
    await _save_credentials(db, 1, username="e2e-user", password="e2e-pass")
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), MAINPAGE_HTML, url=MAINPAGE_URL)  # stored session: dead
    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory())
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(seconds=1), browser_factory=sc.factory())
    await db.commit()
    assert result.get("recovered") == "signed_in", result
    slot = await _slot(db, 1)
    assert slot.state == "ACTIVE" and slot.logged_in_by == "auto-recovery"
    state = __import__("json").loads(settings.decrypt_value(slot.storage_state_encrypted))
    assert any(c["name"] == "ASP.NET_SessionId" and c["value"] == "e2e" for c in state["cookies"])
    assert any(c["name"] == "submitted" and c["value"] == "e2e-user%2Fe2e-pass" for c in state["cookies"])  # the saved values were what the form sent
    assert "login completed by auto-recovery" in slot.state_reason
    assert login_source.state == "ACTIVE"


async def test_block_at_sign_in_halts_source_and_slot(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
    await _save_credentials(db, 1)
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), MAINPAGE_HTML, url=MAINPAGE_URL)
    reg = FakeRegistry(BLOCKED)
    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory(), registry_factory=lambda: reg)
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(seconds=1), browser_factory=sc.factory(), registry_factory=lambda: reg)
    await db.commit()
    assert result.get("halted") is True and ("complete", 1) not in reg.log
    assert login_source.state == "HALTED" and (await _slot(db, 1)).state == "HALTED"


async def test_verification_when_reopening_stored_session_waits_for_a_human(db, login_source, monkeypatch):
    """A CAPTCHA on the stored session means no credentials are submitted behind it."""
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
    await _save_credentials(db, 1)
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), "<html><body>Please verify you are human</body></html>")
    reg = FakeRegistry(AUTHENTICATED)
    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory(), registry_factory=lambda: reg)
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(seconds=1), browser_factory=sc.factory(), registry_factory=lambda: reg)
    await db.commit()
    assert result.get("verification") is True and reg.log == []
    assert (await _slot(db, 1)).state == "NEEDS_HUMAN_LOGIN"


async def test_sign_in_failure_counts_as_an_attempt_and_backs_off(db, login_source, monkeypatch):
    """A browser that cannot even open the login page must not make the task retry every five
    minutes: the attempt is recorded and the next one waits."""
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
    await _save_credentials(db, 1)
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), MAINPAGE_HTML, url=MAINPAGE_URL)

    class ExplodingRegistry(FakeRegistry):
        async def start(self, *a, **k):
            raise RuntimeError("navigation timeout")

    reg = ExplodingRegistry(AUTHENTICATED)
    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory(), registry_factory=lambda: reg)
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(seconds=1), browser_factory=sc.factory(), registry_factory=lambda: reg)
    await db.commit()
    assert "failed" in result and result["sign_in"]["verdict"] == "error"
    record = (login_source.config_json or {})[recovery_key(1)]
    assert record["attempts"] == 1 and datetime.fromisoformat(record["next_attempt_at"]) == t0 + timedelta(seconds=1) + timedelta(minutes=15)


async def test_registry_start_closes_browser_when_login_page_fails(monkeypatch):
    """A half-started login session (Chromium up, login page never loaded) is closed, not leaked."""
    from scraper.auth import browser_login

    closed = []

    class BrokenSession(browser_login.LoginSession):
        async def start(self):
            raise RuntimeError("navigation timeout")

        async def close(self):
            closed.append(self.slot_number)

    monkeypatch.setattr(browser_login, "LoginSession", BrokenSession)
    reg = browser_login.LoginSessionRegistry()
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        await reg.start("PakistanLawSite", 1, "http://127.0.0.1:1/login")
    assert closed == [1] and reg.get("PakistanLawSite") is None
