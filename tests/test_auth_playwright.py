"""Auth / Playwright (tests 16–21) and the four-tier PakistanLawSite pipeline (Cursor command §4)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select, update

from scraper.auth.session_manager import (
    BrowserDisconnected,
    ContinuityRunner,
    PageResult,
    PlaywrightBrowser,
    SearchFormSubmissionError,
    SessionLock,
    SessionLockHeld,
    SessionManager,
    raise_for_verdict,
)
from scraper.config import settings
from scraper.database import SessionLocal
from scraper.fetchers import canonical_text_hash
from scraper.models import (
    BrowserSessionSlot,
    Citation,
    CrawlCoverage,
    CrawlFrontier,
    Judgment,
    Notification,
    ScraperSource,
    ScraperStaging,
    SearchFormMap,
    SourceProvenance,
)
from scraper.security import ExplicitBlock, VerificationRequired
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline, build_values, seed_frontier
from scraper.tasks.promotion import promote_judgment_staging, promote_staging_records
from scraper.tasks.search_map import map_search_form
from tests.fixtures import BLOCK_PAGE, LOGIN_PAGE, VERIFICATION_PAGE, BrowserScript, FakeBrowser, citation_search_hybrid_html, judgment_html, results_html, search_form_html

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
        self.case_description_modal_payload = None

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
        if "#ExceptionResponseScreen1" in script:
            return self.case_description_modal_payload
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


def _script_with_rows(rows):
    """Form-tier script: citation page 1 of the reporter volume lists `rows`
    (citation, title, court, detail_url); every other page is empty."""
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), search_form_html())

    def search(values, browser):
        if int(values.get("page") or 0) == 1:
            return PageResult(url="https://www.pakistanlawsite.com/r", html=results_html([(c, t, court, u) for c, t, court, u in rows]))
        return PageResult(url="https://www.pakistanlawsite.com/r", html=results_html([]))

    sc.default_search = search
    return sc


def _tier1_current_year(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", datetime.now(timezone.utc).year)


def _notes_only_detail_html(citation: str, title: str) -> str:
    return (
        "<html><body><a href='/logout'>Logout</a>"
        f"<h2>Citation Name: {citation}</h2>"
        f"<h3>{title}</h3>"
        "<h4>Notes on Cases</h4>"
        "<p>Important principles noted by the digest editor.</p>"
        "</body></html>"
    )


def _modal_full_judgment_text(citation: str, title: str) -> str:
    intro = [
        citation,
        "IN THE SUPREME COURT OF PAKISTAN",
        "Before Qazi Faez Isa, CJ and Syed Mansoor Ali Shah, JJ",
        title,
        "Decided on 12th March 2024",
        "JUDGMENT",
    ]
    body = [
        (
            "The appellant challenged the conviction under section 302 of the Pakistan Penal Code, 1860. "
            "After hearing learned counsel for both sides and examining the record in detail, the bench held "
            "that the prosecution had failed to establish guilt beyond reasonable doubt."
        )
        for _ in range(80)
    ]
    return "\n".join(intro + body)


# --------------------------------------------------------------------------- 16
async def test_human_login_stores_encrypted_storage_state(db, login_source):
    mgr = SessionManager(db, login_source)
    slot = await mgr.save_storage_state(1, STATE, by="advocate")
    assert slot.state == "ACTIVE" and slot.storage_state_encrypted.startswith("gAAAA")
    assert "abc" not in slot.storage_state_encrypted
    assert mgr.load_storage_state(slot) == STATE
    assert login_source.state == "ACTIVE"


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
    assert result.metadata["requested_url"] == "https://www.pakistanlawsite.com/Login/CitationSearch"
    assert result.metadata["final_url"] == "https://www.pakistanlawsite.com/Login/CitationSearch"
    goto_call = next(c for c in page.calls if c[0] == "goto")
    assert goto_call[2]["wait_until"] == "domcontentloaded"
    assert goto_call[2]["timeout"] == settings.PLAYWRIGHT_TIMEOUT_MS


async def test_playwright_goto_refuses_metadata_and_off_allow_list_urls():
    page = _FakePage()
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page
    with pytest.raises(ExplicitBlock, match="url_policy"):
        await browser.goto("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(ExplicitBlock, match="url_policy"):
        await browser.goto("https://example.com/")
    assert not any(call[0] == "goto" for call in page.calls)


async def test_playwright_goto_rewrites_login_check_to_requested_reference_case_url():
    page = _FakePage()

    async def _goto_with_redirect(url, **kwargs):
        page.calls.append(("goto", url, kwargs))
        page.url = "https://www.pakistanlawsite.com/login/check"
        return SimpleNamespace(status=200, headers={"content-type": "text/html"})

    page.goto = _goto_with_redirect
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    requested_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2006K247&&court= &&Row=0 &&bookName=undefined"
    result = await browser.goto(requested_url)

    assert result.url == requested_url
    assert result.metadata["requested_url"] == requested_url
    assert result.metadata["final_url"] == "https://www.pakistanlawsite.com/login/check"
    assert result.metadata["url_rewritten_from_login_check"] is True


async def test_playwright_goto_rewrites_login_check_when_case_html_contains_citation_name():
    page = _FakePage(html="<html><body><h2>Citation Name: PLD 2024 SC 101</h2></body></html>")

    async def _goto_with_redirect(url, **kwargs):
        page.calls.append(("goto", url, kwargs))
        page.url = "https://www.pakistanlawsite.com/login/check"
        return SimpleNamespace(status=200, headers={"content-type": "text/html"})

    page.goto = _goto_with_redirect
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    requested_url = "https://www.pakistanlawsite.com/case/247"
    result = await browser.goto(requested_url)

    assert result.url == requested_url
    assert result.metadata["requested_url"] == requested_url
    assert result.metadata["final_url"] == "https://www.pakistanlawsite.com/login/check"
    assert result.metadata["url_rewritten_from_login_check"] is True


async def test_playwright_goto_captures_case_description_modal_text_when_requested():
    page = _FakePage(html="<html><body>detail</body></html>")
    page.case_description_modal_payload = {
        "case_description_selector_found": True,
        "case_description_modal_found": True,
        "case_description_modal_text": "Before Justice A and Justice B, JJ\nFull text here",
        "case_description_modal_text_length": 52,
    }
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    result = await browser.goto(
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2006K247",
        capture_case_description_modal=True,
    )

    assert result.metadata["case_description_selector_found"] is True
    assert result.metadata["case_description_modal_found"] is True
    assert result.metadata["case_description_modal_text"].startswith("Before Justice A")


async def test_capture_case_description_modal_waits_for_full_text_ready_markers():
    page = _FakePage(html="<html><body>detail</body></html>")
    page.case_description_modal_payload = {
        "case_description_selector_found": True,
        "case_description_modal_found": True,
        "case_description_modal_text": "modal shell only",
        "case_description_modal_text_length": 251,
    }
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    meta = await browser._capture_case_description_modal()

    assert set(meta) == {
        "case_description_selector_found",
        "case_description_modal_found",
        "case_description_modal_text",
        "case_description_modal_text_length",
    }
    modal_eval_call = next(call for call in page.calls if call[0] == "evaluate" and "#ExceptionResponseScreen1" in call[1])
    script = modal_eval_call[1]
    assert "for (let i = 0; i < 80; i += 1)" in script
    assert "await sleep(150)" in script
    assert "text.length >= 2000" in script
    assert "/Before.+/i.test(text)" in script
    assert "/CLC|SCMR|PLD/i.test(text)" in script


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


async def test_playwright_submit_search_rejects_unsafe_mapped_control_before_fill():
    page = _FakePage()
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page

    with pytest.raises(SearchFormSubmissionError, match="unsupported control kind"):
        await browser.submit_search(
            {"fields": {"keyword": {"selector": "#gridFilter", "kind": "hidden"}}},
            {"keyword": "test"},
        )

    assert not any(call[0] == "fill" for call in page.calls)


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


async def test_search_form_map_prefers_citation_form_over_grid_filters(db, login_source):
    m = await map_search_form(db, login_source, citation_search_hybrid_html())

    assert set(m.fields) >= {"reporter", "year", "citation", "keyword", "submit"}
    assert m.fields["reporter"]["selector"] == "#reporter"
    assert m.fields["citation"]["selector"] == "#citationPage"
    assert m.fields["keyword"]["selector"] == "#queryText"
    assert "citation_filter" not in {field["name"] for field in m.fields["_all"]}
    assert "title_filter" not in {field["name"] for field in m.fields["_all"]}
    assert m.result_layout["row_selector"] == "table#citationGrid tr"
    assert m.pagination["next_selector"] == "#nextResults"
    assert build_values({"fields": m.fields}, {"reporter": "PLD", "year": 2024}, {"page_no": 3}) == {
        "reporter": "PLD",
        "year": "2024",
        "citation": "3",
    }


async def test_paged_query_retires_with_explicit_unmapped_role_reason(db, login_source):
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    frontier = CrawlFrontier(
        source_name="PakistanLawSite",
        tier=3,
        query_key="t3:constitutional",
        query_json={"keyword": "constitutional"},
        cursor_json={"page": 1},
    )

    await pipeline.run_paged_query(frontier, {"fields": {"submit": {"selector": "#go", "kind": "submit"}}}, max_pages=1)

    assert frontier.status == "retired"
    assert frontier.last_error == "search map cannot express keyword query; missing usable role: keyword"


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


async def test_pipeline_uses_case_description_modal_text_and_pins_deterministic_extraction(db, login_source, monkeypatch):
    _tier1_current_year(monkeypatch)
    await _activate(db, login_source)
    citation = "PLD 2024 SC 777"
    title = "Modal versus Headnote"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2006K777&&court= &&Row=0 &&bookName=undefined"
    sc = _script_with_rows([(citation, title, "Supreme Court", detail_url)])
    sc.routes[("goto", detail_url)] = lambda _browser: PageResult(
        url=detail_url,
        html=_notes_only_detail_html(citation, title),
        status=200,
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": True,
            "case_description_modal_found": True,
            "case_description_modal_text": _modal_full_judgment_text(citation, title),
            "case_description_modal_text_length": 32000,
        },
    )
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=5, max_probes_per_volume=5)
    await db.commit()
    await r.aclose()
    assert stats["staged"] == 1
    staging = (await db.execute(select(ScraperStaging))).scalars().first()
    assert staging is not None
    assert staging.extraction_engine == "deterministic"
    assert (staging.reconciled_json or {}).get("document_type") == "full_judgment"
    assert "Qazi Faez Isa" in ((staging.reconciled_json or {}).get("judge_names") or [])
    assert "Notes on Cases" not in (staging.raw_text or "")[:200]


async def test_pipeline_marks_notes_only_reference_case_as_headnote_and_promotion_quarantines(db, login_source, monkeypatch):
    _tier1_current_year(monkeypatch)
    await _activate(db, login_source)
    citation = "PLD 2024 SC 778"
    title = "Notes Only Case"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2006K778&&court= &&Row=0 &&bookName=undefined"
    sc = _script_with_rows([(citation, title, "Supreme Court", detail_url)])
    sc.routes[("goto", detail_url)] = lambda _browser: PageResult(
        url=detail_url,
        html=_notes_only_detail_html(citation, title),
        status=200,
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": False,
            "case_description_modal_found": False,
            "case_description_modal_text": None,
            "case_description_modal_text_length": 0,
        },
    )
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=5, max_probes_per_volume=5)
    await db.commit()
    await r.aclose()
    assert stats["staged"] == 1
    staging = (await db.execute(select(ScraperStaging))).scalars().first()
    assert staging is not None
    assert (staging.reconciled_json or {}).get("document_type") == "headnote"
    assert await promote_judgment_staging(db, staging) == "quarantined"


async def test_preserve_and_extract_uses_modal_text_identity_to_upgrade_headnote_html_duplicate(db, login_source):
    await _activate(db, login_source)
    citation = "PLD 2024 SC 901"
    title = "Upgrade from modal body"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2006K901&&court= &&Row=0 &&bookName=undefined"
    page_html = _notes_only_detail_html(citation, title)
    modal_text = _modal_full_judgment_text(citation, title)
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    row = {"citation": citation, "title": title, "court": "Supreme Court", "detail_url": detail_url}

    first = await pipeline.preserve_and_extract(
        PageResult(
            url=detail_url,
            html=page_html,
            metadata={
                "requested_url": detail_url,
                "final_url": detail_url,
                "case_description_selector_found": False,
                "case_description_modal_found": False,
                "case_description_modal_text": None,
                "case_description_modal_text_length": 0,
            },
        ),
        {"tier": 4, "row_index": 0},
        row,
    )
    second = await pipeline.preserve_and_extract(
        PageResult(
            url=detail_url,
            html=page_html,
            metadata={
                "requested_url": detail_url,
                "final_url": detail_url,
                "case_description_selector_found": True,
                "case_description_modal_found": True,
                "case_description_modal_text": modal_text,
                "case_description_modal_text_length": len(modal_text),
            },
        ),
        {"tier": 4, "row_index": 1},
        row,
    )

    assert first == "staged"
    assert second == "staged"
    rows = (await db.execute(select(ScraperStaging).order_by(ScraperStaging.created_at.asc()))).scalars().all()
    assert len(rows) == 2
    headnote_row = next(r for r in rows if (r.reconciled_json or {}).get("document_type") == "headnote")
    full_row = next(r for r in rows if (r.reconciled_json or {}).get("document_type") == "full_judgment")
    assert headnote_row.content_hash != full_row.content_hash
    assert "Notes on Cases" in (headnote_row.raw_text or "")
    assert "Notes on Cases" not in (full_row.raw_text or "")[:200]
    assert "Qazi Faez Isa" in ((full_row.reconciled_json or {}).get("judge_names") or [])
    headnote_prov = (
        await db.execute(select(SourceProvenance).where(SourceProvenance.id == headnote_row.provenance_id))
    ).scalars().first()
    modal_prov = (
        await db.execute(select(SourceProvenance).where(SourceProvenance.id == full_row.provenance_id))
    ).scalars().first()
    assert headnote_prov is not None and modal_prov is not None
    assert modal_prov.parent_id == headnote_prov.id
    assert modal_prov.document_kind == "case_description_modal"
    assert modal_prov.content_kind == "text"


async def test_pipeline_persists_renewed_session_cookies_back_to_the_slot(db, login_source, monkeypatch):
    """Each job opens a fresh browser from the slot's stored state. If that state stays frozen at
    login time while the site renews its cookies, the next job opens with stale cookies and the login
    is lost about once an hour. The live state must be written back after every window."""
    _tier1_current_year(monkeypatch)
    mgr = await _activate(db, login_source)
    before = (await mgr.slot(1)).storage_state_hash
    rows = [("PLD 2024 SC 6001", "Case 6001", "Supreme Court", "https://www.pakistanlawsite.com/case/6001")]
    sc = _script_with_rows(rows)
    sc.page(("goto", rows[0][3]), judgment_html(rows[0][0], title=rows[0][1]))
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=5, max_probes_per_volume=5)
    await db.commit()
    await r.aclose()
    assert stats["staged"] == 1
    assert stats["session_state_refreshes"] >= 1
    slot = await mgr.slot(1)
    assert slot.storage_state_hash != before
    assert slot.state == "ACTIVE"
    refreshed = mgr.load_storage_state(slot)
    assert any(c.get("name") == "renewed" for c in refreshed["cookies"])
    assert any(c.get("name") == "sid" for c in refreshed["cookies"])  # the human login's cookie is kept


def test_raise_for_verdict_records_only_the_landed_path_never_query_or_tokens():
    from scraper.auth.session_manager import LoginRequired, safe_url_for_record

    page = PageResult(
        url="https://www.pakistanlawsite.com/Login/MainPage?ReturnUrl=%2FLogin%2FCheck&token=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345&sid=verysecretsessionid#frag",
        html=LOGIN_PAGE,
    )
    with pytest.raises(LoginRequired) as exc:
        raise_for_verdict(page)
    text = str(exc.value)
    assert "landed on https://www.pakistanlawsite.com/Login/MainPage" in text
    for leaked in ("token=", "sid=", "verysecretsessionid", "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345", "ReturnUrl", "#frag"):
        assert leaked not in text
    assert safe_url_for_record("") == ""
    assert safe_url_for_record("not a url?x=1") == "not a url"


async def test_refresh_storage_state_ignores_inactive_or_empty_states(db, login_source):
    mgr = await _activate(db, login_source)
    slot = await mgr.slot(1)
    before = slot.storage_state_hash
    assert await mgr.refresh_storage_state(1, {"cookies": [], "origins": []}, expected_hash=before) is None
    assert await mgr.refresh_storage_state(1, STATE, expected_hash=before) is None  # identical state: nothing to write
    assert (await mgr.slot(1)).storage_state_hash == before
    slot.state = "NEEDS_HUMAN_LOGIN"
    await db.flush()
    renewed = {"cookies": [{"name": "x", "value": "y", "domain": "d", "path": "/"}], "origins": []}
    assert await mgr.refresh_storage_state(1, renewed, expected_hash=before) is None
    assert (await mgr.slot(1)).storage_state_hash == before


async def test_refresh_storage_state_never_overwrites_a_newer_human_login_or_an_admin_clear(db, login_source):
    """A job that opened its browser with state A must not write its renewed cookies over state B that a
    human login stored meanwhile, nor over a slot an admin cleared (Codex review on #111)."""
    mgr = await _activate(db, login_source)
    opened_with = (await mgr.slot(1)).storage_state_hash
    await db.commit()
    # Meanwhile: a fresh human login in another session stores state B.
    newer = {"cookies": [{"name": "sid", "value": "fresh-login", "domain": "www.pakistanlawsite.com", "path": "/"}], "origins": []}
    async with SessionLocal() as other:
        other_source = (await other.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().one()
        await SessionManager(other, other_source).save_storage_state(1, newer, by="human")
        await other.commit()
    stale_renewal = {"cookies": [{"name": "sid", "value": "abc", "domain": "www.pakistanlawsite.com", "path": "/"}, {"name": "renewed", "value": "r1", "domain": "www.pakistanlawsite.com", "path": "/"}], "origins": []}
    assert await mgr.refresh_storage_state(1, stale_renewal, expected_hash=opened_with) is None
    await db.commit()
    async with SessionLocal() as verify:
        row = (await verify.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite", BrowserSessionSlot.slot_number == 1))).scalars().one()
        assert json.loads(settings.decrypt_value(row.storage_state_encrypted)) == newer
        current = row.storage_state_hash
    # With the current hash the refresh is accepted.
    assert await mgr.refresh_storage_state(1, stale_renewal, expected_hash=current) is not None
    await db.commit()
    # Meanwhile: an admin clears the slot.
    async with SessionLocal() as other:
        await other.execute(update(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite", BrowserSessionSlot.slot_number == 1).values(storage_state_encrypted=None, storage_state_hash=None, state="EMPTY", state_reason="cleared by admin"))
        await other.commit()
    assert await mgr.refresh_storage_state(1, stale_renewal, expected_hash=(await mgr.slot(1)).storage_state_hash) is None
    await db.commit()
    async with SessionLocal() as verify:
        row = (await verify.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite", BrowserSessionSlot.slot_number == 1))).scalars().one()
        assert row.state == "EMPTY" and row.storage_state_encrypted is None


async def test_login_scraping_disabled_outside_chambers(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "cloud")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory())
    with pytest.raises(PermissionError):
        await pipeline.run()


async def _nosleep(_s):
    return None


async def test_lock_release_never_deletes_another_workers_lock():
    """Release is a compare-and-delete: a lock that expired and was taken by another worker in the
    meantime must survive the first worker's release."""
    r = aioredis.from_url(settings.REDIS_URL)
    key = "corpus:login_session_lock:PakistanLawSite"
    await r.delete(key)
    lock1 = SessionLock("PakistanLawSite", r)
    await lock1.acquire()
    await r.set(key, "another-workers-token", ex=60)  # the TTL ran out and a second worker took it
    await lock1.release()
    assert (await r.get(key)) == b"another-workers-token"
    await r.delete(key)
    await r.aclose()


async def test_page_capture_falls_back_to_forms_when_content_outlasts_the_timeout(monkeypatch):
    """The live CitationSearch DOM is 10-16 MB; when page.content() outlasts the Playwright timeout
    the forms (and the logout link) are captured on their own instead of the page being lost."""
    monkeypatch.setattr(settings, "PLAYWRIGHT_TIMEOUT_MS", 100)  # floor is 5 s
    page = _FakePage()

    async def slow_content():
        page.calls.append(("content",))
        await asyncio.sleep(30)
        return page.html

    async def evaluate(script, *args):
        page.calls.append(("evaluate", script, args))
        assert "document.forms" in script
        return '<html><head><title>Citation Search</title></head><body><a href="/Login/Logout">Logout</a><form id="f"><select name="book"></select></form></body></html>'

    page.content = slow_content
    page.evaluate = evaluate
    browser = PlaywrightBrowser(STATE, 1, base_url=settings.PLS_BASE_URL)
    browser._page = page
    result = await browser.goto(settings.PLS_SEARCH_URL)
    assert "<form" in result.html and "Logout" in result.html
    assert result.classify().kind == "ok"


async def test_paged_query_resumes_from_saved_next_url_not_page_one(db, login_source, monkeypatch):
    """Specification 3.5: a Tier 3 row that stopped after 10 pages continues from its saved
    next_url on the next run; it never resubmits the query and reprocesses page 1."""
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 0)
    monkeypatch.setattr(settings, "PLS_TIER3_VOCABULARY", "")
    await _activate(db, login_source)
    next_url = "https://www.pakistanlawsite.com/r?page=11"
    db.add(CrawlFrontier(source_name="PakistanLawSite", tier=3, query_key="t3:limitation", query_json={"keyword": "limitation"}, cursor_json={"page": 11, "row_index": 0, "next_url": next_url}, priority=80))
    await db.commit()
    sc = BrowserScript()
    sc.page(("goto", settings.PLS_SEARCH_URL), search_form_html())
    submitted = []

    def search(values, browser):
        submitted.append(values)
        return PageResult(url="https://www.pakistanlawsite.com/r", html=results_html([("PLD 2024 SC 1", "Page one", "Supreme Court", "https://www.pakistanlawsite.com/case/1")]))

    sc.default_search = search
    sc.page(("goto", next_url), results_html([("PLD 2024 SC 11", "Page eleven", "Supreme Court", "https://www.pakistanlawsite.com/case/11")]))
    sc.page(("goto", "https://www.pakistanlawsite.com/case/11"), judgment_html("PLD 2024 SC 11", title="Page eleven"))
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    stats = await pipeline.run(max_queries=5, max_probes_per_volume=5)
    await db.commit()
    await r.aclose()
    assert submitted == []  # the query was not resubmitted
    assert stats["staged"] == 1
    staged = (await db.execute(select(ScraperStaging))).scalars().first()
    assert staged.extracted_citation == "PLD 2024 SC 11"
    fr = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.tier == 3))).scalars().first()
    assert fr.status == "done" and fr.yield_count == 1


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


async def test_human_login_autofills_saved_credentials_and_can_submit(fixture_server):
    from scraper.auth.browser_login import LoginSessionRegistry

    fixture_server.add(
        "/login",
        "<html><body><form onsubmit=\"document.getElementById('out').textContent='ok:'+u.value+'/'+p.value;return false;\">"
        "<input id=u name='Login.UserName'><input id=p name='Login.Password' type='password'>"
        "<input id=terms type=checkbox name='chkAgree'><input id=remember type=checkbox name='RememberMe'><button id=signin type=submit>Sign in</button></form><div id=out></div></body></html>",
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
        assert await sess._page.evaluate("() => remember.checked") is True  # every box in the form is ticked
        assert sess.last_autofill and sess.last_autofill["applied"] and sess.last_autofill["submitted"]
    finally:
        await reg.cancel("PakistanLawSite")

