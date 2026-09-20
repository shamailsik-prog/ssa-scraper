"""Auth / Playwright (tests 16–21) and the four-tier PakistanLawSite pipeline (Cursor command §4)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select

from scraper.auth.session_manager import (
    BrowserDisconnected,
    ContinuityRunner,
    PageResult,
    PlaywrightBrowser,
    SessionLock,
    SessionLockHeld,
    SessionManager,
    raise_for_verdict,
)
from scraper.config import settings
from scraper.models import BrowserSessionSlot, CrawlCoverage, CrawlFrontier, Judgment, Notification, ScraperStaging, SearchFormMap
from scraper.security import ExplicitBlock, VerificationRequired
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline, build_values, seed_frontier
from scraper.tasks.promotion import promote_staging_records
from scraper.tasks.search_map import map_search_form
from tests.fixtures import BLOCK_PAGE, LOGIN_PAGE, VERIFICATION_PAGE, BrowserScript, FakeBrowser, judgment_html, results_html, search_form_html

STATE = {"cookies": [{"name": "sid", "value": "abc", "domain": "www.pakistanlawsite.com", "path": "/"}], "origins": []}


class _FakeNavigationWait:
    def __init__(self, page):
        self.page = page

    async def __aenter__(self):
        self.page.calls.append(("expect_navigation.enter",))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.page.calls.append(("expect_navigation.exit",))
        return False


class _FakePage:
    def __init__(self, html="<html><body>ok</body></html>", url="https://www.pakistanlawsite.com/Login/CitationSearch"):
        self.url = url
        self.html = html
        self.calls = []
        self.keyboard = SimpleNamespace(press=self._press)
        self.dom_shape = {
            "forms": 1,
            "inputs": 2,
            "has_archivedpatient_grid": False,
            "archivedpatient_rows": 0,
            "has_logout": True,
            "body_preview": "ok",
        }
        self.archived_grid_snapshot = None

    async def goto(self, url, **kwargs):
        self.calls.append(("goto", url, kwargs))
        self.url = url
        return SimpleNamespace(status=200, headers={"content-type": "text/html"})

    async def content(self):
        self.calls.append(("content",))
        return self.html

    async def click(self, selector):
        self.calls.append(("click", selector))

    async def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    async def select_option(self, selector, value):
        self.calls.append(("select_option", selector, value))

    async def check(self, selector):
        self.calls.append(("check", selector))

    async def uncheck(self, selector):
        self.calls.append(("uncheck", selector))

    async def _press(self, key):
        self.calls.append(("press", key))

    def expect_navigation(self, **kwargs):
        self.calls.append(("expect_navigation", kwargs))
        return _FakeNavigationWait(self)

    async def evaluate(self, script, *args):
        self.calls.append(("evaluate", script, args))
        if "has_archivedpatient_grid" in script:
            return dict(self.dom_shape)
        if "citation_idx" in script and "row_count" in script:
            if not self.archived_grid_snapshot:
                return None
            headers = list(self.archived_grid_snapshot.get("headers") or [])
            rows = list(self.archived_grid_snapshot.get("rows") or [])
            return {
                "headers": headers,
                "row_count": len(rows),
                "citation_idx": 0,
                "title_idx": 1,
                "court_idx": 2,
                "effective_headers": ["Citation", "Title", "Court", "Read"],
                "next_url": self.archived_grid_snapshot.get("next_url"),
                "body_preview": self.archived_grid_snapshot.get("body_preview"),
                "has_logout": bool(self.archived_grid_snapshot.get("has_logout")),
            }
        if "capStart" in script and "bodyRows" in script:
            if not self.archived_grid_snapshot:
                return []
            if args and isinstance(args[0], dict):
                start = int(args[0].get("start") or 0)
                end = int(args[0].get("end") or len(self.archived_grid_snapshot.get("rows") or []))
            else:
                start = int(args[0]) if args else 0
                end = int(args[1]) if len(args) > 1 else len(self.archived_grid_snapshot.get("rows") or [])
            rows = list(self.archived_grid_snapshot.get("rows") or [])
            return rows[start:end]
        if "archivedpatientGrid" in script:
            return self.archived_grid_snapshot
        return None


async def _activate(db, source, slots=(1,)):
    mgr = SessionManager(db, source)
    for n in slots:
        await mgr.save_storage_state(n, STATE, by="test")
    source.state = "ACTIVE"
    await db.commit()
    return mgr


def _script_with_results(n_hits: int, citation_prefix="PLD 2024 SC"):
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), search_form_html())

    def search(values, browser):
        page_no = int(values.get("page") or 0)
        if 1 <= page_no <= n_hits:
            cit = f"{citation_prefix} {page_no}"
            return PageResult(url="https://www.pakistanlawsite.com/r", html=results_html([(cit, f"Party {page_no} versus State", "Supreme Court", f"https://www.pakistanlawsite.com/case/{page_no}")]))
        return PageResult(url="https://www.pakistanlawsite.com/r", html=results_html([]))

    sc.default_search = search
    for i in range(1, n_hits + 1):
        sc.page(("goto", f"https://www.pakistanlawsite.com/case/{i}"), judgment_html(f"{citation_prefix} {i}", title=f"Party {i} versus State"))
    return sc


# --------------------------------------------------------------------------- 16
async def test_human_login_stores_encrypted_storage_state(db, login_source):
    mgr = SessionManager(db, login_source)
    slot = await mgr.save_storage_state(1, STATE, by="advocate")
    assert slot.state == "ACTIVE" and slot.storage_state_encrypted.startswith("gAAAA")
    assert "abc" not in slot.storage_state_encrypted
    assert mgr.load_storage_state(slot) == STATE
    assert login_source.state == "ACTIVE"


async def test_saved_credentials_encrypt_decrypt_round_trip(db, login_source):
    mgr = SessionManager(db, login_source)
    slot = await mgr.save_login_credentials(1, "advocate@example.com", "Sup3rSecret!", by="operator")
    assert slot.login_username_encrypted and slot.login_username_encrypted.startswith("gAAAA")
    assert slot.login_password_encrypted and slot.login_password_encrypted.startswith("gAAAA")
    assert "advocate@example.com" not in slot.login_username_encrypted
    assert "Sup3rSecret!" not in slot.login_password_encrypted
    assert mgr.load_login_credentials(slot) == {"username": "advocate@example.com", "password": "Sup3rSecret!"}
    await mgr.clear_login_credentials(1)
    assert mgr.load_login_credentials(slot) is None


async def test_saved_credentials_malformed_tokens_fail_closed(db, login_source):
    mgr = SessionManager(db, login_source)
    slot = await mgr.slot(1)
    slot.login_username_encrypted = "not-a-fernet-token"
    slot.login_password_encrypted = "also-not-a-token"
    await db.flush()
    assert mgr.load_login_credentials(slot) is None


async def test_human_login_browser_stream_and_completion(db, login_source, fixture_server, monkeypatch):
    """Real Playwright: a streamed login page, frames arrive, credentials typed by the 'human',
    completion exports storage state into the slot. Passwords are never stored in plaintext."""
    from scraper.auth.browser_login import LoginSessionRegistry

    fixture_server.add("/login", LOGIN_PAGE)
    fixture_server.add("/Login/CitationSearch", search_form_html())
    monkeypatch.setattr(settings, "PLS_SEARCH_URL", fixture_server.url("/Login/CitationSearch"))
    reg = LoginSessionRegistry()
    sess = await reg.start("PakistanLawSite", 2, fixture_server.url("/login"), started_by="advocate")
    try:
        frame = await sess.next_frame(timeout=15)
        assert frame is not None and frame["type"] == "frame" and frame["data"]
        status = await sess.is_authenticated()
        assert status["authenticated"] is False  # password field present → not yet logged in
        await sess.input_event({"kind": "navigate", "url": fixture_server.url("/Login/CitationSearch")})
        await sess._page.evaluate("() => { document.cookie = 'sid=humanlogin; path=/'; localStorage.setItem('k','v'); }")
        result = await reg.complete("PakistanLawSite", SessionManager(db, login_source))
        assert result["stored"] is True and result["slot"] == 2
        slot = (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.slot_number == 2))).scalars().first()
        state = json.loads(settings.decrypt_value(slot.storage_state_encrypted))
        assert any(c["name"] == "sid" and c["value"] == "humanlogin" for c in state["cookies"])
        with pytest.raises(Exception):
            await sess.input_event({"kind": "navigate", "url": "https://evil.example.com/"})
    finally:
        await reg.cancel("PakistanLawSite")


async def test_human_login_autofills_saved_credentials_and_can_submit(fixture_server):
    from scraper.auth.browser_login import LoginSessionRegistry

    fixture_server.add(
        "/login",
        "<html><body><form onsubmit=\"document.getElementById('out').textContent='ok:'+u.value+'/'+p.value;return false;\">"
        "<input id=u name='Login.UserName'><input id=p name='Login.Password' type='password'>"
        "<input id=terms type=checkbox name='agreeTerms'><button id=signin type=submit>Sign in</button></form><div id=out></div></body></html>",
    )
    reg = LoginSessionRegistry()
    sess = await reg.start(
        "PakistanLawSite",
        1,
        fixture_server.url("/login"),
        saved_credentials={"username": "stored-user", "password": "stored-pass"},
        auto_complete=True,
    )
    try:
        await sess._page.wait_for_timeout(300)
        data = await sess._page.evaluate("() => ({u: u.value, p: p.value, terms: terms.checked, out: document.getElementById('out').textContent})")
        assert data == {"u": "stored-user", "p": "stored-pass", "terms": True, "out": "ok:stored-user/stored-pass"}
        assert sess.last_autofill and sess.last_autofill["applied"] and sess.last_autofill["submitted"]
    finally:
        await reg.cancel("PakistanLawSite")


async def test_is_authenticated_rejects_public_mainpage(fixture_server):
    from scraper.auth.browser_login import LoginSessionRegistry

    fixture_server.add("/login", LOGIN_PAGE)
    fixture_server.add(
        "/Login/CitationSearch",
        "<html><body><h1>MainPage</h1><a href='/Login/MainPage'>Sign in</a><form action='/Login/Login'><input name='Login.UserName'></form></body></html>",
    )
    reg = LoginSessionRegistry()
    sess = await reg.start("PakistanLawSite", 1, fixture_server.url("/login"))
    try:
        check = await sess.is_authenticated_for(fixture_server.url("/Login/CitationSearch"))
        assert check["authenticated"] is False
        assert check["expected_match"] is True
    finally:
        await reg.cancel("PakistanLawSite")


async def test_is_authenticated_accepts_citation_search_surface(fixture_server):
    from scraper.auth.browser_login import LoginSessionRegistry

    fixture_server.add("/login", LOGIN_PAGE)
    fixture_server.add("/Login/CitationSearch", search_form_html())
    reg = LoginSessionRegistry()
    sess = await reg.start("PakistanLawSite", 1, fixture_server.url("/login"))
    try:
        check = await sess.is_authenticated_for(fixture_server.url("/Login/CitationSearch"))
        assert check["authenticated"] is True
        assert check["expected_match"] is True
        assert check["url"].endswith("/Login/CitationSearch")
    finally:
        await reg.cancel("PakistanLawSite")


async def test_playwright_goto_uses_domcontentloaded_without_networkidle_wait():
    page = _FakePage()
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    result = await browser.goto("https://www.pakistanlawsite.com/Login/CitationSearch")

    assert result.status == 200
    goto_call = next(c for c in page.calls if c[0] == "goto")
    assert goto_call[2]["wait_until"] == "domcontentloaded"
    assert goto_call[2]["timeout"] == settings.PLAYWRIGHT_TIMEOUT_MS


async def test_playwright_goto_uses_compact_table_guard_for_oversized_archived_grid():
    page = _FakePage(html="<html><body>oversized</body></html>")
    page.dom_shape = {
        "forms": 0,
        "inputs": settings.PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD + 100,
        "has_archivedpatient_grid": True,
        "archivedpatient_rows": 2,
        "has_logout": True,
        "body_preview": "citation table",
    }
    page.archived_grid_snapshot = {
        "headers": ["Citation", "Title", "Court", "Read"],
        "rows": [
            {
                "citation": "PLD 2024 SC 11",
                "title": "A v B",
                "court": "Supreme Court",
                "detail_url": "https://www.pakistanlawsite.com/case/11",
                "pdf_url": None,
            }
        ],
        "next_url": None,
        "body_preview": "citation table",
        "has_logout": True,
    }
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    result = await browser.goto("https://www.pakistanlawsite.com/Login/CitationSearch")

    assert result.metadata["content_guard"] == "archivedpatientGrid_compact"
    assert "id=\"archivedpatientGrid\"" in result.html
    assert not any(call[0] == "content" for call in page.calls)


async def test_playwright_goto_compact_grid_headers_are_stable_when_hash_column_exists():
    page = _FakePage(html="<html><body>oversized</body></html>")
    page.dom_shape = {
        "forms": 0,
        "inputs": settings.PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD + 100,
        "has_archivedpatient_grid": True,
        "archivedpatient_rows": 1,
        "has_logout": True,
        "body_preview": "citation table",
    }
    page.archived_grid_snapshot = {
        "headers": ["#", "Citation", "Title", "Court", "Read"],
        "rows": [
            {
                "citation": "PLD 2024 SC 11",
                "title": "A v B",
                "court": "Supreme Court",
                "detail_url": "https://www.pakistanlawsite.com/case/11",
                "pdf_url": None,
            }
        ],
        "next_url": None,
        "body_preview": "citation table",
        "has_logout": True,
    }
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    result = await browser.goto("https://www.pakistanlawsite.com/Login/CitationSearch")

    assert "<th>#</th>" not in result.html
    assert "<th>Citation</th><th>Title</th><th>Court</th><th>Read</th>" in result.html


async def test_playwright_goto_compact_snapshot_timeout_returns_partial_rows(monkeypatch):
    class _SlowBatchPage(_FakePage):
        async def evaluate(self, script, *args):
            payload = args[0] if args else {}
            start = int(payload.get("start") or 0) if isinstance(payload, dict) else int(args[0])
            if "capStart" in script and "bodyRows" in script and start >= 1:
                raise asyncio.TimeoutError("simulated batch timeout")
            return await super().evaluate(script, *args)

    monkeypatch.setattr(settings, "PLS_ARCHIVED_GRID_SNAPSHOT_TIMEOUT_MS", 20)
    monkeypatch.setattr(settings, "PLS_ARCHIVED_GRID_BATCH_ROWS", 1)
    page = _SlowBatchPage(html="<html><body>oversized</body></html>")
    page.dom_shape = {
        "forms": 0,
        "inputs": settings.PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD + 100,
        "has_archivedpatient_grid": True,
        "archivedpatient_rows": 2,
        "has_logout": True,
        "body_preview": "citation table",
    }
    page.archived_grid_snapshot = {
        "headers": ["Citation", "Title", "Court", "Read"],
        "rows": [
            {
                "citation": "PLD 2024 SC 11",
                "title": "A v B",
                "court": "Supreme Court",
                "detail_url": "https://www.pakistanlawsite.com/case/11",
                "pdf_url": None,
            },
            {
                "citation": "PLD 2024 SC 12",
                "title": "C v D",
                "court": "Supreme Court",
                "detail_url": "https://www.pakistanlawsite.com/case/12",
                "pdf_url": None,
            },
        ],
        "next_url": None,
        "body_preview": "citation table",
        "has_logout": True,
    }
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    result = await browser.goto("https://www.pakistanlawsite.com/Login/CitationSearch")

    assert result.metadata["content_guard"] == "archivedpatientGrid_compact"
    assert result.metadata["snapshot_timed_out"] is True
    assert result.metadata["rows"] == 1
    assert "PLD 2024 SC 11" in result.html
    assert "PLD 2024 SC 12" not in result.html
    assert not any(call[0] == "content" for call in page.calls)


async def test_playwright_submit_search_waits_for_domcontentloaded_navigation():
    page = _FakePage(html=results_html([("PLD 2024 SC 1", "Party v State", "Supreme Court", "https://www.pakistanlawsite.com/case/1")]))
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page
    search_map = {
        "fields": {
            "keyword": {"selector": "#keyword", "kind": "text"},
            "submit": {"selector": "#submit", "kind": "button"},
        }
    }

    result = await browser.submit_search(search_map, {"keyword": "test"})

    assert result.url == page.url
    assert ("fill", "#keyword", "test") in page.calls
    nav_call = next(c for c in page.calls if c[0] == "expect_navigation")
    assert nav_call[1]["wait_until"] == "domcontentloaded"
    assert nav_call[1]["timeout"] == settings.PLAYWRIGHT_TIMEOUT_MS


async def test_human_login_typing_box_text_named_keys_and_focus_info(fixture_server):
    """Phones have no hardware keyboard: the dashboard sends text and named keys, and learns which
    field has focus after each tap (type and label only, never a value). Reopening the login for the
    same slot reuses the open browser; a different slot replaces it."""
    from scraper.auth.browser_login import LoginSessionError, LoginSessionRegistry

    fixture_server.add(
        "/login",
        "<html><body style='margin:0'><form onsubmit=\"document.getElementById('out').textContent='submitted:'+u.value+'/'+p.value;return false;\">"
        "<input id=u name=u placeholder='User Name' style='position:absolute;left:100px;top:100px;width:200px;height:30px'>"
        "<input id=p name=p type=password placeholder='Password' style='position:absolute;left:100px;top:160px;width:200px;height:30px'>"
        "<button type=submit style='position:absolute;left:100px;top:220px'>Sign in</button></form><div id=out></div></body></html>",
    )
    reg = LoginSessionRegistry()
    sess = await reg.start("PakistanLawSite", 1, fixture_server.url("/login"), viewport={"width": 480, "height": 800})
    try:
        assert sess.viewport == {"width": 480, "height": 800}
        assert await reg.start("PakistanLawSite", 1, fixture_server.url("/login")) is sess
        assert await reg.start("PakistanLawSite", 1, fixture_server.url("/login"), viewport={"width": 1280, "height": 800}) is sess
        assert sess.viewport == {"width": 1280, "height": 800} and sess._page.viewport_size == {"width": 1280, "height": 800}
        assert await sess.next_frame(timeout=15) is not None

        async def tap(x, y):
            await sess.input_event({"kind": "mouse", "type": "mouseMoved", "x": x, "y": y})
            await sess.input_event({"kind": "mouse", "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            return await sess.input_event({"kind": "mouse", "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

        info = await tap(150, 115)
        assert info["editable"] and info["label"] == "u" and "value" not in info
        await sess.input_event({"kind": "text", "text": "advox"})
        await sess.input_event({"kind": "press", "key": "Backspace"})
        await sess.input_event({"kind": "text", "text": "legal"})
        info = await sess.input_event({"kind": "press", "key": "Tab"})  # Tab moves focus: the dashboard must learn it is now a password field
        assert info["input_type"] == "password"
        await sess.input_event({"kind": "text", "text": "secret1"})
        await sess.input_event({"kind": "press", "key": "Enter"})
        await sess._page.wait_for_timeout(300)
        assert await sess._page.evaluate("() => [u.value, p.value, document.getElementById('out').textContent]") == ["advolegal", "secret1", "submitted:advolegal/secret1"]
        with pytest.raises(LoginSessionError):
            await sess.input_event({"kind": "press", "key": "F13"})
        other = await reg.start("PakistanLawSite", 2, fixture_server.url("/login"))
        assert other is not sess and sess.status == "closed"
    finally:
        await reg.cancel("PakistanLawSite")


# --------------------------------------------------------------------------- 17
async def test_verification_page_marks_needs_human_login_without_solving(db, login_source):
    mgr = await _activate(db, login_source)
    sc = BrowserScript()
    sc.page(("goto", "https://www.pakistanlawsite.com/case/9"), VERIFICATION_PAGE)
    runner = ContinuityRunner(mgr, sc.factory(), sleep=_nosleep)

    async def op(browser):
        page = await browser.goto("https://www.pakistanlawsite.com/case/9")
        raise_for_verdict(page)
        return page

    with pytest.raises(VerificationRequired):
        await runner.run(op)
    slot = await mgr.slot(1)
    assert slot.state == "NEEDS_HUMAN_LOGIN"
    assert login_source.state == "PAUSED"
    notes = (await db.execute(select(Notification.code))).scalars().all()
    assert "NEEDS_HUMAN_LOGIN" in notes
    assert all(("captcha" not in (k[0] if isinstance(k, tuple) else str(k)).lower()) for k, _ in sc.log)  # nothing attempted to solve


# --------------------------------------------------------------------------- 18
async def test_disconnect_waits_30s_reconnects_same_slot_same_cursor(db, login_source):
    mgr = await _activate(db, login_source, slots=(1, 2))
    sc = BrowserScript()
    key = ("goto", "https://www.pakistanlawsite.com/case/5")
    sc.page(key, judgment_html("PLD 2024 SC 5"))
    sc.fail_once(key, BrowserDisconnected("socket hang up"))
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    runner = ContinuityRunner(mgr, sc.factory(), sleep=fake_sleep)
    cursor = {"page_no": 5, "row_index": 0}
    seen_cursors = []

    async def op(browser):
        seen_cursors.append(dict(cursor))
        return await browser.goto(f"https://www.pakistanlawsite.com/case/{cursor['page_no']}")

    page = await runner.run(op)
    assert page.status == 200
    assert slept == [30]
    slots_used = [slot for (_k, slot) in sc.log]
    assert slots_used == [1, 1]  # same slot both times
    assert seen_cursors == [cursor, cursor]  # same cursor, never page one
    assert (await mgr.slot(1)).reconnect_count == 1


# --------------------------------------------------------------------------- 19
async def test_reconnect_failure_uses_alternate_slot_same_cursor(db, login_source):
    mgr = await _activate(db, login_source, slots=(1, 2))
    sc = BrowserScript()
    key = ("goto", "https://www.pakistanlawsite.com/case/7")
    sc.page(key, judgment_html("PLD 2024 SC 7"))
    sc.fail_once(key, BrowserDisconnected("first"))
    sc.fail_once(key, BrowserDisconnected("reconnect failed too"))
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    runner = ContinuityRunner(mgr, sc.factory(), sleep=fake_sleep)
    cursor = {"page_no": 7}

    async def op(browser):
        return await browser.goto(f"https://www.pakistanlawsite.com/case/{cursor['page_no']}")

    page = await runner.run(op)
    assert page.status == 200 and slept == [30]
    assert [slot for (_k, slot) in sc.log] == [1, 1, 2]
    assert (login_source.config_json or {}).get("current_slot") == 2
    # no automatic recovery to the primary afterwards (no try_recover_primary)
    cur = await mgr.current_slot()
    assert cur.slot_number == 2


# --------------------------------------------------------------------------- 20
@pytest.mark.parametrize("html,status", [(BLOCK_PAGE, 200), ("<html><body>Forbidden</body></html>", 403), ("<html><body>automated access detected</body></html>", 200)])
async def test_explicit_block_halts_without_slot_switch(db, login_source, html, status):
    mgr = await _activate(db, login_source, slots=(1, 2))
    sc = BrowserScript()
    sc.page(("goto", "https://www.pakistanlawsite.com/case/1"), html, status=status)
    runner = ContinuityRunner(mgr, sc.factory(), sleep=_nosleep)

    async def op(browser):
        page = await browser.goto("https://www.pakistanlawsite.com/case/1")
        raise_for_verdict(page)
        return page

    with pytest.raises(ExplicitBlock):
        await runner.run(op)
    assert login_source.state == "HALTED" and login_source.requires_admin_review
    assert (await mgr.slot(1)).state == "HALTED"
    assert (await mgr.slot(2)).state == "ACTIVE"  # alternate untouched: no bypass attempted
    assert [slot for (_k, slot) in sc.log] == [1]
    codes = (await db.execute(select(Notification.code))).scalars().all()
    assert "SOURCE_HALTED" in codes


# --------------------------------------------------------------------------- 21
async def test_second_concurrent_login_session_worker_refused():
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    lock1 = SessionLock("PakistanLawSite", r)
    lock2 = SessionLock("PakistanLawSite", r)
    await lock1.acquire()
    try:
        with pytest.raises(SessionLockHeld):
            await lock2.acquire()
    finally:
        await lock1.release()
    await lock2.acquire()
    await lock2.release()
    await r.aclose()


async def test_lock_refresh_requires_same_owner_token():
    r = aioredis.from_url(settings.REDIS_URL)
    key = "corpus:login_session_lock:PakistanLawSite"
    await r.delete(key)
    lock = SessionLock("PakistanLawSite", r)
    await lock.acquire()
    try:
        await r.set(key, "other-worker-token", ex=3600)
        with pytest.raises(SessionLockHeld):
            await lock.refresh()
    finally:
        await r.delete(key)
        await r.aclose()


async def test_charge_page_refreshes_lock_heartbeat(db, login_source):
    await _activate(db, login_source)
    calls = {"refresh": 0}

    class DummyLock:
        async def refresh(self):
            calls["refresh"] += 1

    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    pipeline._session_lock = DummyLock()
    await pipeline._charge_page()
    assert calls["refresh"] == 1


async def test_pipeline_refuses_when_lock_held(db, login_source, monkeypatch):
    await _activate(db, login_source)
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    other = SessionLock("PakistanLawSite", r)
    await other.acquire()
    try:
        pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), redis_client=r, sleep=_nosleep)
        with pytest.raises(SessionLockHeld):
            await pipeline.run()
    finally:
        await other.release()
        await r.aclose()


# --------------------------------------------------------------------------- search map (6.3) and tiers (6.4 / 6.5)
async def test_search_form_map_deterministic_and_verified(db, login_source):
    m = await map_search_form(db, login_source, search_form_html())
    assert m.map_version == 1 and m.verified_against_dom and m.mapped_by == "deterministic"
    assert set(m.fields) >= {"reporter", "year", "page", "keyword", "submit"}
    assert m.result_layout["row_selector"] == "table#results tr" and m.result_layout["columns"] == {"citation": 0, "title": 1, "court": 2}
    vals = build_values({"fields": m.fields}, {"reporter": "PLD", "year": 2024}, {"page_no": 3})
    assert vals == {"reporter": "PLD", "year": "2024", "page": "3"}
    m2 = await map_search_form(db, login_source, search_form_html())
    assert m2.map_version == 2 and not m.is_active


async def test_tier1_volume_closes_after_40_misses_and_frontier_is_truth(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", datetime.now().year)
    monkeypatch.setattr(settings, "VOLUME_END_GAP", 40)
    await _activate(db, login_source)
    sc = _script_with_results(3)
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=5, max_probes_per_volume=100)
    await db.commit()
    await r.aclose()
    year = datetime.now().year
    cov = (await db.execute(select(CrawlCoverage).where(CrawlCoverage.reporter == "PLD", CrawlCoverage.year == year))).scalars().first()
    assert cov.volume_state == "closed" and cov.consecutive_misses == 40 and cov.highest_page_seen == 3 and cov.judgments_found == 3
    fr = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.tier == 1))).scalars().first()
    assert fr.status == "done" and fr.cursor_json["page_no"] == 44
    staged = (await db.execute(select(func.count()).select_from(ScraperStaging))).scalar()
    assert staged == 3 and stats["volumes_closed"] == 1
    # Tier 4 row was seeded for the current year
    assert (await db.execute(select(CrawlFrontier).where(CrawlFrontier.tier == 4))).scalars().first() is not None
    # promotion yields exactly three judgments with the Tier-1 route preserved on provenance
    counts = await promote_staging_records()
    assert counts["promoted"] == 3
    async with __import__("scraper.database", fromlist=["SessionLocal"]).SessionLocal() as db2:
        assert (await db2.execute(select(func.count()).select_from(Judgment))).scalar() == 3


async def test_same_judgment_via_tier1_and_tier2_is_one_staging_row(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", datetime.now().year)
    monkeypatch.setattr(settings, "VOLUME_END_GAP", 2)
    await _activate(db, login_source)
    sc = _script_with_results(1)
    # Tier 2 query (statute 302 PPC) returns the same judgment
    from scraper.models import Statute, StatuteSection

    st = Statute(name="Pakistan Penal Code, 1860", short_name="PPC")
    db.add(st)
    await db.flush()
    db.add(StatuteSection(statute_id=st.id, section_number="302"))
    await db.commit()
    original_search = sc.default_search

    def search(values, browser):
        if values.get("keyword", "").startswith("PPC section 302") or values.get("statute"):
            return PageResult(url="https://www.pakistanlawsite.com/r", html=results_html([("PLD 2024 SC 1", "Party 1 versus State", "Supreme Court", "https://www.pakistanlawsite.com/case/1")]))
        return original_search(values, browser)

    sc.default_search = search
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=10, max_probes_per_volume=10)
    await db.commit()
    await r.aclose()
    assert (await db.execute(select(func.count()).select_from(ScraperStaging))).scalar() == 1
    assert stats["duplicates"] >= 1
    from scraper.models import SourceProvenance

    prov = (await db.execute(select(SourceProvenance).where(SourceProvenance.content_kind == "html", SourceProvenance.source_url.like("%/case/1")))).scalars().first()
    tiers = {route.get("tier") for route in prov.routes}
    assert {1, 2} <= tiers


async def test_search_map_goes_stale_after_five_parse_failures(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", datetime.now().year)
    monkeypatch.setattr(settings, "VOLUME_END_GAP", 40)
    await _activate(db, login_source)
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), search_form_html())
    sc.default_search = lambda values, browser: PageResult(url="https://www.pakistanlawsite.com/r", html="<html><body><a href='/logout'>Logout</a><div>layout changed completely</div></body></html>")
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await pipeline.run(max_queries=1, max_probes_per_volume=6)
    await db.commit()
    await r.aclose()
    m = (await db.execute(select(SearchFormMap).where(SearchFormMap.is_active.is_(True)))).scalars().first()
    assert m.stale and m.consecutive_parse_failures >= 5
    codes = (await db.execute(select(Notification.code))).scalars().all()
    assert "search_map_stale" in codes


async def test_pipeline_extracts_archivedpatient_grid_rows_without_search_form(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 0)
    await _activate(db, login_source)
    parsed = urlsplit(settings.PLS_SEARCH_URL)
    expected_detail_url = f"{parsed.scheme}://{parsed.netloc}/Login/ReferenceCaseLawSearch?CaseName=2006K247&&court= &&Row=0 &&bookName=undefined"
    sc = BrowserScript()
    sc.page(
        ("goto", settings.PLS_SEARCH_URL),
        """
        <html><body><a href="/logout">Logout</a>
        <table id="archivedpatientGrid">
          <thead><tr><th>#</th><th>Citation</th><th>Title</th><th>Court</th><th>Read</th></tr></thead>
          <tbody>
            <tr>
              <td>1</td>
              <td>PLD 2024 SC 247</td>
              <td>Alpha versus State</td>
              <td>Supreme Court</td>
              <td><input type="button" casetypeid="2006K247" class="btn btn-success courtWiseSearchBtn" value="Read"></td>
            </tr>
          </tbody>
        </table></body></html>
        """,
    )
    sc.page(("goto", expected_detail_url), judgment_html("PLD 2024 SC 247", title="Alpha versus State"))
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=5, max_probes_per_volume=5)
    await db.commit()
    await r.aclose()
    assert stats["surface_mode"] == "citation_grid"
    assert stats["rows"] == 1
    assert stats["staged"] == 1
    assert stats["url_less_skips"] == 0
    assert (await db.execute(select(func.count()).select_from(ScraperStaging))).scalar() == 1


async def test_pipeline_forces_citation_grid_map_v1_on_archived_grid_surface(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 0)
    await _activate(db, login_source)
    stale_map = SearchFormMap(
        source_name="PakistanLawSite",
        map_version=7,
        fields={"_all": []},
        result_layout={"row_selector": "table#whatsNewTable tr", "columns": {"citation": 0, "title": 1, "court": 2}, "detail_link_selector": "a[href]"},
        page_size=None,
        pagination={},
        detail_layout={"detail_link_selector": "a[href]"},
        limits={},
        dom_hash="0" * 64,
        mapped_by="deterministic",
        verified_against_dom=True,
        is_active=True,
    )
    db.add(stale_map)
    await db.commit()
    sc = BrowserScript()
    sc.page(
        ("goto", settings.PLS_SEARCH_URL),
        """
        <html><body><a href="/logout">Logout</a>
        <table id="archivedpatientGrid">
          <thead><tr><th>#</th><th>Citation</th><th>Title</th><th>Court</th><th>Read</th></tr></thead>
          <tbody><tr><td>1</td><td>PLD 2024 SC 401</td><td>A v B</td><td>Supreme Court</td><td><a href="https://www.pakistanlawsite.com/case/401">Read</a></td></tr></tbody>
        </table></body></html>
        """,
    )
    sc.page(("goto", "https://www.pakistanlawsite.com/case/401"), judgment_html("PLD 2024 SC 401", title="A v B"))
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=2, max_probes_per_volume=2)
    await db.commit()
    await r.aclose()

    assert stats["surface_mode"] == "citation_grid"
    active = (await db.execute(select(SearchFormMap).where(SearchFormMap.source_name == "PakistanLawSite", SearchFormMap.is_active.is_(True)))).scalars().first()
    assert active is not None
    assert active.mapped_by == "forced_citation_grid_v1"
    assert active.result_layout["row_selector"] == "table#archivedpatientGrid tbody tr"
    assert active.result_layout["columns"] == {"citation": 1, "title": 2, "court": 3}


async def test_pipeline_citation_grid_respects_detail_fetch_cap(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 0)
    monkeypatch.setattr(settings, "PLS_CITATION_GRID_MAX_DETAIL", 2)
    await _activate(db, login_source)
    sc = BrowserScript()
    sc.page(
        ("goto", settings.PLS_SEARCH_URL),
        """
        <html><body><a href="/logout">Logout</a>
        <table id="archivedpatientGrid">
          <thead><tr><th>#</th><th>Citation</th><th>Title</th><th>Court</th><th>Read</th></tr></thead>
          <tbody>
            <tr><td>1</td><td>PLD 2024 SC 501</td><td>A v B</td><td>Supreme Court</td><td><a href="https://www.pakistanlawsite.com/case/501">Read</a></td></tr>
            <tr><td>2</td><td>PLD 2024 SC 502</td><td>C v D</td><td>Supreme Court</td><td><a href="https://www.pakistanlawsite.com/case/502">Read</a></td></tr>
            <tr><td>3</td><td>PLD 2024 SC 503</td><td>E v F</td><td>Supreme Court</td><td><a href="https://www.pakistanlawsite.com/case/503">Read</a></td></tr>
          </tbody>
        </table></body></html>
        """,
    )
    sc.page(("goto", "https://www.pakistanlawsite.com/case/501"), judgment_html("PLD 2024 SC 501", title="A v B"))
    sc.page(("goto", "https://www.pakistanlawsite.com/case/502"), judgment_html("PLD 2024 SC 502", title="C v D"))
    sc.page(("goto", "https://www.pakistanlawsite.com/case/503"), judgment_html("PLD 2024 SC 503", title="E v F"))
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=2, max_probes_per_volume=2)
    await db.commit()
    await r.aclose()

    assert stats["surface_mode"] == "citation_grid"
    assert stats["detail_candidates"] == 3
    assert stats["detail_cap"] == 2
    assert stats["detail_cap_skipped"] == 1
    assert stats["staged"] == 2
    assert (await db.execute(select(func.count()).select_from(ScraperStaging))).scalar() == 2


async def test_pipeline_citation_grid_raises_when_rows_have_no_detail_urls(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 0)
    await _activate(db, login_source)
    sc = BrowserScript()
    sc.page(
        ("goto", settings.PLS_SEARCH_URL),
        """
        <html><body><a href="/logout">Logout</a>
        <table id="archivedpatientGrid">
          <thead><tr><th>#</th><th>Citation</th><th>Title</th><th>Court</th><th>Read</th></tr></thead>
          <tbody>
            <tr><td>1</td><td>PLD 2024 SC 301</td><td>No URL Case</td><td>Supreme Court</td><td>Read</td></tr>
          </tbody>
        </table></body></html>
        """,
    )
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    with pytest.raises(RuntimeError, match="none had a detail URL"):
        await pipeline.run(max_queries=5, max_probes_per_volume=5)
    await r.aclose()
    assert pipeline.stats["rows"] == 1
    assert pipeline.stats["staged"] == 0
    assert pipeline.stats["url_less_skips"] == 1


async def test_login_scraping_disabled_outside_chambers(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "cloud")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory())
    with pytest.raises(PermissionError):
        await pipeline.run()


async def _nosleep(_s):
    return None
