"""
Human login rendered inside the dashboard (Amendment §9; Cursor command §2 item 6.1).

The service launches a server-side Chromium, opens the source's login page and streams the
viewport to the operator over a WebSocket using the Chrome DevTools screencast. Mouse and
keyboard events from the operator are replayed into the page. The operator types credentials
and completes any verification. Operators may also save per-slot username/password on the
trusted host; those values are encrypted at rest and used to pre-fill login fields on demand.
When the operator confirms, the service checks the page is authenticated, exports cookies and
localStorage as Playwright storage state, encrypts it and stores it in the chosen slot.

Credentials never appear in prompts, plain database fields or logs.
CAPTCHA / verification pages are never solved by code — they are shown to the human.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from scraper.config import settings
from scraper.security import classify_response

logger = logging.getLogger(__name__)


class LoginSessionError(RuntimeError):
    pass


# Named keys the dashboard's typing box sends (phones have no hardware keyboard and their on-screen
# keyboards do not produce usable key events, so the dashboard sends text and named keys instead).
NAMED_KEYS: Dict[str, tuple] = {
    "Enter": (13, "\r"), "Tab": (9, None), "Backspace": (8, None), "Delete": (46, None), "Escape": (27, None),
    "ArrowLeft": (37, None), "ArrowUp": (38, None), "ArrowRight": (39, None), "ArrowDown": (40, None),
    "Home": (36, None), "End": (35, None), "PageUp": (33, None), "PageDown": (34, None),
}


@dataclass
class LoginSession:
    source_name: str
    slot_number: int
    login_url: str
    started_by: str = "operator"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    frames: "asyncio.Queue[Dict[str, Any]]" = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    status: str = "starting"
    last_url: Optional[str] = None
    _pw: Any = None
    _browser: Any = None
    _context: Any = None
    _page: Any = None
    _cdp: Any = None
    viewport: Dict[str, int] = field(default_factory=lambda: {"width": 1280, "height": 800})
    last_autofill: Optional[Dict[str, Any]] = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        # Headless Chromium still supports Page.startScreencast, so no display server is needed on the
        # host; the human "sees" the headed experience through the dashboard stream.
        self._browser = await self._pw.chromium.launch(headless=settings.PLAYWRIGHT_HEADLESS, executable_path=settings.PLAYWRIGHT_EXECUTABLE_PATH or None)
        self._context = await self._browser.new_context(viewport=self.viewport)
        self._page = await self._context.new_page()
        self._cdp = await self._context.new_cdp_session(self._page)
        self._cdp.on("Page.screencastFrame", self._on_frame)
        await self._cdp.send("Page.enable")
        await self._cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": self.viewport["width"], "maxHeight": self.viewport["height"], "everyNthFrame": 2})
        await self._page.goto(self.login_url, wait_until="domcontentloaded")
        # Static login pages may not repaint after load, so push one explicit frame
        # to avoid `next_frame()` timing out on first operator connect.
        await self.snapshot()
        self.last_url = self._page.url
        self.status = "awaiting_human"

    def _on_frame(self, params: Dict[str, Any]) -> None:
        frame = {"type": "frame", "data": params.get("data"), "metadata": params.get("metadata", {}), "url": self._page.url if self._page else None}
        try:
            if self.frames.full():
                self.frames.get_nowait()
            self.frames.put_nowait(frame)
        except asyncio.QueueFull:
            pass
        if params.get("sessionId") is not None:
            asyncio.create_task(self._ack(params.get("sessionId")))

    async def snapshot(self) -> None:
        """Capture the current page as one frame. The screencast only emits when the page repaints,
        so a static page shows nothing to an operator who (re)connects or resizes; this fills that gap."""
        try:
            # A screenshot asked for while the page is navigating (a submitted login form) can wait
            # on the debug channel indefinitely; bound it, the next real frame follows anyway.
            shot = await asyncio.wait_for(self._cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 60}), timeout=5.0)
            self._on_frame({"data": shot.get("data"), "metadata": {"deviceWidth": self.viewport["width"], "deviceHeight": self.viewport["height"]}})
        except Exception as exc:  # page navigating or slow; the next real frame will follow
            logger.debug("snapshot skipped: %s", exc)

    async def _ack(self, session_id) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    async def next_frame(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            if self.frames.empty():
                await self.snapshot()
            try:
                return await asyncio.wait_for(self.frames.get(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                continue

    async def input_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Replay an operator input event. Accepted:
        mouse {type: mousePressed|mouseReleased|mouseMoved|mouseWheel, x, y, button, clickCount, deltaX, deltaY};
        key {type: keyDown|keyUp|char, key, code, text, modifiers} (hardware keyboards);
        text {text} inserts a string into the focused element (on-screen keyboards, paste);
        press {key} presses one named key (Enter, Tab, Backspace, ...);
        navigate {url} (same host only).
        Returns a description of the focused element after a click, so the dashboard can show what
        the operator is typing into (and mask its own typing box for password fields)."""
        kind = event.get("kind")
        info: Optional[Dict[str, Any]] = None
        if kind == "mouse":
            await self._cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": event.get("type", "mouseMoved"),
                    "x": float(event.get("x", 0)),
                    "y": float(event.get("y", 0)),
                    "button": event.get("button", "left"),
                    "clickCount": int(event.get("clickCount", 1)),
                    "deltaX": float(event.get("deltaX", 0)),
                    "deltaY": float(event.get("deltaY", 0)),
                },
            )
            if event.get("type") == "mouseReleased":
                info = await self.focused_element()
        elif kind == "key":
            payload = {"type": event.get("type", "keyDown"), "modifiers": int(event.get("modifiers", 0))}
            for k in ("key", "code", "text", "unmodifiedText", "windowsVirtualKeyCode", "nativeVirtualKeyCode"):
                if event.get(k) is not None:
                    payload[k] = event[k]
            await self._cdp.send("Input.dispatchKeyEvent", payload)
        elif kind == "text":
            text = str(event.get("text", ""))[:2000]
            if text:
                await self._cdp.send("Input.insertText", {"text": text})
        elif kind == "press":
            name = str(event.get("key", ""))
            if name not in NAMED_KEYS:
                raise LoginSessionError(f"unknown key {name!r}")
            vk, text = NAMED_KEYS[name]
            down: Dict[str, Any] = {"type": "keyDown" if text else "rawKeyDown", "key": name, "code": name, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
            if text:
                down["text"] = text
                down["unmodifiedText"] = text
            await self._cdp.send("Input.dispatchKeyEvent", down)
            await self._cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": name, "code": name, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
            if name in ("Tab", "Enter"):  # focus may have moved (Tab) or the page may have submitted (Enter)
                info = await self.focused_element()
        elif kind == "navigate":
            target = str(event.get("url", ""))
            if urlsplit(target).hostname != urlsplit(self.login_url).hostname:
                raise LoginSessionError("navigation outside the source host is not permitted")
            await self._page.goto(target, wait_until="domcontentloaded")
        self.last_url = self._page.url
        return info

    async def focused_element(self) -> Dict[str, Any]:
        """Tag, input type and label of the element that currently has focus in the page. Values are
        never read: only what kind of field it is, so the operator knows where their typing goes."""
        try:
            return await self._page.evaluate(
                """() => { const a = document.activeElement; if (!a || a === document.body) return {tag: null};
                  const t = (a.getAttribute('type') || (a.tagName === 'TEXTAREA' ? 'textarea' : '')).toLowerCase();
                  const lab = (a.labels && a.labels[0]) ? a.labels[0].textContent.trim() : '';
                  const label = a.getAttribute('aria-label') || lab || a.getAttribute('name') || a.id || a.getAttribute('title') || '';
                  const editable = ['INPUT','TEXTAREA'].includes(a.tagName) && !['checkbox','radio','submit','button','hidden','file'].includes(t) || a.isContentEditable;
                  return {tag: a.tagName.toLowerCase(), input_type: t, label: String(label).slice(0, 60), editable: !!editable}; }"""
            )
        except Exception as exc:  # page navigating, frame detached, ...
            return {"tag": None, "error": str(exc)[:80]}

    async def is_authenticated(self) -> Dict[str, Any]:
        return await self._auth_verdict()

    async def _auth_verdict(self, expected_url: Optional[str] = None) -> Dict[str, Any]:
        html = await self._page.content()
        current_url = self._page.url
        verdict = classify_response(200, html, current_url)
        low = html.lower()
        path = (urlsplit(current_url).path or "").lower()

        has_password = 'type="password"' in low or "type='password'" in low
        has_login_form = any(
            marker in low
            for marker in (
                "mainloginform",
                "name=\"login.username\"",
                "name='login.username'",
                "name=\"login.password\"",
                "name='login.password'",
                "action=\"/login/login\"",
                "action='/login/login'",
                "id=\"login\"",
                "id='login'",
            )
        )
        has_logout = "logout" in low or "log off" in low or "sign out" in low
        public_login_url = any(
            piece in path
            for piece in ("/login/mainpage", "/login/login", "/login/main", "/login/index")
        )

        expected_match = True
        expected_path = ""
        if expected_url:
            expected_path = (urlsplit(expected_url).path or "").lower().rstrip("/")
            current_path = path.rstrip("/")
            expected_match = current_path == expected_path or current_path.startswith(expected_path + "/")

        search_surface = "citationsearch" in path or "id=\"searchform\"" in low or "id='searchform'" in low
        positive_auth_signal = has_logout or search_surface
        blocked_by_login_surface = has_password or has_login_form or public_login_url
        ok = verdict.kind == "ok" and positive_auth_signal and not blocked_by_login_surface and expected_match
        return {
            "authenticated": ok,
            "verdict": verdict.kind,
            "detail": verdict.detail,
            "url": current_url,
            "expected_url": expected_url,
            "expected_match": expected_match,
        }

    async def is_authenticated_for(self, expected_url: str) -> Dict[str, Any]:
        """Prove the session can reach an authenticated target URL without a login bounce."""
        try:
            await self._page.goto(
                expected_url,
                wait_until="domcontentloaded",
                timeout=settings.PLAYWRIGHT_TIMEOUT_MS,
            )
        except Exception as exc:
            return {
                "authenticated": False,
                "verdict": "navigation_error",
                "detail": str(exc)[:300],
                "url": self._page.url if self._page else None,
                "expected_url": expected_url,
                "expected_match": False,
            }
        self.last_url = self._page.url
        return await self._auth_verdict(expected_url=expected_url)

    async def resize(self, viewport: Dict[str, int]) -> None:
        """Change the streamed browser's size (operator switched between phone and desktop layout)."""
        if viewport == self.viewport:
            return
        self.viewport = dict(viewport)
        # Restart the screencast at the new size first, then resize: the repaint the resize causes is
        # then the first frame of the new stream (a static page paints nothing on its own).
        try:
            await self._cdp.send("Page.stopScreencast")
        except Exception:
            pass
        await self._cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": self.viewport["width"], "maxHeight": self.viewport["height"], "everyNthFrame": 2})
        await self._page.set_viewport_size(self.viewport)
        await self.snapshot()

    async def export_storage_state(self) -> Dict[str, Any]:
        return await self._context.storage_state()

    async def apply_saved_credentials(self, username: str, password: str, *, auto_complete: bool = False) -> Dict[str, Any]:
        """Fill known PakistanLawSite login fields from encrypted server-side credentials.

        No credentials are logged or returned. The stream remains open so a human can complete
        CAPTCHA/verification or fix changed selectors.
        """
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            result = {"applied": False, "submitted": False, "reason": "missing username/password"}
            self.last_autofill = result
            return result
        result = await self._page.evaluate(
            """({username, password, autoComplete}) => {
                const pick = (selectors) => {
                  for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) return el;
                  }
                  return null;
                };
                const fire = (el) => {
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                };
                const user = pick([
                  "input[name='Login.UserName']",
                  "input[name='username']",
                  "input[name='user']",
                  "input[id='Login_UserName']",
                  "input[id='username']",
                  "input[id='user']",
                  "input[type='email']",
                  "input[autocomplete='username']",
                  "input[name*='user' i]",
                  "input[id*='user' i]"
                ]);
                const pass = pick([
                  "input[name='Login.Password']",
                  "input[name='password']",
                  "input[id='Login_Password']",
                  "input[id='password']",
                  "input[type='password']",
                  "input[autocomplete='current-password']",
                  "input[name*='pass' i]",
                  "input[id*='pass' i]"
                ]);
                let checkedTerms = false;
                // The live sign-in form's terms box is <input type=checkbox class=agreeBox> with no name
                // or id, and the page refuses the submit until it is ticked; failing that, the only
                // checkbox inside the sign-in form is the terms box.
                const formBoxes = pass && pass.form ? Array.from(pass.form.querySelectorAll("input[type='checkbox']")) : [];
                const terms = pick([
                  "input[type='checkbox'].agreeBox",
                  "input[type='checkbox'][class*='agree' i]",
                  "input[type='checkbox'][name*='agree' i]",
                  "input[type='checkbox'][id*='agree' i]",
                  "input[type='checkbox'][name*='term' i]",
                  "input[type='checkbox'][id*='term' i]"
                ]) || (formBoxes.length === 1 ? formBoxes[0] : null);
                if (user) {
                  user.focus();
                  user.value = username;
                  fire(user);
                }
                if (pass) {
                  pass.focus();
                  pass.value = password;
                  fire(pass);
                }
                if (terms && !terms.checked) {
                  terms.checked = true;
                  fire(terms);
                  checkedTerms = true;
                }
                let submitted = false;
                if (autoComplete && user && pass) {
                  const submit = pick([
                    "button[type='submit']",
                    "input[type='submit']",
                    "button[name*='sign' i]",
                    "button[id*='sign' i]",
                    "button[name*='login' i]",
                    "button[id*='login' i]"
                  ]);
                  if (submit) {
                    submit.click();
                    submitted = true;
                  } else if (pass.form) {
                    pass.form.requestSubmit ? pass.form.requestSubmit() : pass.form.submit();
                    submitted = true;
                  }
                }
                return {
                  applied: !!(user && pass),
                  submitted,
                  username_field_found: !!user,
                  password_field_found: !!pass,
                  checked_terms: checkedTerms
                };
              }""",
            {"username": username, "password": password, "autoComplete": auto_complete},
        )
        self.last_autofill = result
        await self.snapshot()
        return result

    async def login_error(self) -> Optional[str]:
        """The site's own answer to a refused sign-in, read from the page after the submit settles:
        "invalid credentials", "account already in use", "terms not accepted", or None."""
        try:
            found = await self._page.evaluate(
                """() => {
                    const text = (sel) => { const el = document.querySelector(sel); return el ? (el.textContent || '').trim() : ''; };
                    const html = document.documentElement ? document.documentElement.innerHTML : '';
                    const red = Array.from(document.querySelectorAll('.red')).some((el) => el.offsetParent !== null);
                    return {message: text('#LoginErrorMessage'), multi: html.indexOf('ErrorForMultiLoginAccess') >= 0,
                            inactive: html.indexOf('ErrorForInactiveUserAccount') >= 0, terms: red};
                }"""
            )
        except Exception as exc:
            logger.debug("login_error: %s", exc)
            return None
        if "invalid" in (found.get("message") or "").lower():
            return "invalid credentials"
        if found.get("multi"):
            return "account already in use"
        if found.get("inactive"):
            return "account inactive"
        if found.get("terms"):
            return "terms not accepted"
        return None

    async def settle(self, timeout_ms: Optional[int] = None) -> None:
        """Let a navigation the page just started (a submitted form) reach DOMContentLoaded, so the
        next check reads the page the site answered with rather than the one being left."""
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms or settings.PLAYWRIGHT_TIMEOUT_MS)
        except Exception as exc:
            logger.debug("settle: %s", exc)

    async def close(self) -> None:
        self.status = "closed"
        for step in (
            lambda: self._cdp.send("Page.stopScreencast") if self._cdp else None,
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            try:
                r = step()
                if r is not None:
                    await asyncio.wait_for(r, timeout=15.0)
            except Exception:
                pass


class LoginSessionRegistry:
    """One live human-login session per source per process."""

    def __init__(self):
        self._sessions: Dict[str, LoginSession] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        source_name: str,
        slot_number: int,
        login_url: str,
        started_by: str = "operator",
        viewport: Optional[Dict[str, int]] = None,
        saved_credentials: Optional[Dict[str, str]] = None,
        auto_complete: bool = False,
    ) -> LoginSession:
        """Open a browser for the human login. If one is already open for this source and slot (the
        operator reloaded the dashboard or lost the connection), it is reused rather than refused; a
        different slot replaces the open one."""
        wanted = None
        if viewport:
            wanted = {"width": max(320, min(1920, int(viewport.get("width", 1280)))), "height": max(480, min(1600, int(viewport.get("height", 800))))}
        async with self._lock:
            existing = self._sessions.get(source_name)
            if existing is not None and existing.status != "closed":
                if existing.slot_number == slot_number:
                    existing.status = "awaiting_human"
                    if wanted:
                        await existing.resize(wanted)
                    if saved_credentials:
                        await existing.apply_saved_credentials(saved_credentials.get("username", ""), saved_credentials.get("password", ""), auto_complete=auto_complete)
                    return existing
                await existing.close()
                self._sessions.pop(source_name, None)
            sess = LoginSession(source_name=source_name, slot_number=slot_number, login_url=login_url, started_by=started_by)
            if wanted:
                sess.viewport = wanted
            try:
                await sess.start()
                if saved_credentials:
                    await sess.apply_saved_credentials(saved_credentials.get("username", ""), saved_credentials.get("password", ""), auto_complete=auto_complete)
            except BaseException:
                # A browser launched but the login page never came (navigation timeout, lost page):
                # the session is not registered yet, so close its Playwright objects here or they leak.
                await sess.close()
                raise
            self._sessions[source_name] = sess
            return sess

    def get(self, source_name: str) -> Optional[LoginSession]:
        s = self._sessions.get(source_name)
        return s if s is not None and s.status != "closed" else None

    async def complete(self, source_name: str, manager) -> Dict[str, Any]:
        """Verify authentication, store encrypted storage state in the slot, close the browser."""
        sess = self.get(source_name)
        if sess is None:
            raise LoginSessionError("no open login session")
        if source_name == "PakistanLawSite":
            check = await sess.is_authenticated_for(settings.PLS_SEARCH_URL)
        else:
            check = await sess.is_authenticated()
        if not check["authenticated"]:
            return {"stored": False, **check}
        state = await sess.export_storage_state()
        await manager.save_storage_state(sess.slot_number, state, by=sess.started_by)
        await sess.close()
        self._sessions.pop(source_name, None)
        return {"stored": True, "slot": sess.slot_number, **check}

    async def cancel(self, source_name: str) -> bool:
        sess = self._sessions.pop(source_name, None)
        if sess is None:
            return False
        await sess.close()
        return True

    def status(self) -> Dict[str, Any]:
        return {name: {"status": s.status, "slot": s.slot_number, "started_at": s.started_at.isoformat(), "url": s.last_url} for name, s in self._sessions.items() if s.status != "closed"}


registry = LoginSessionRegistry()
