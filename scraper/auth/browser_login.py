"""
Human login rendered inside the dashboard (Amendment §9; Cursor command §2 item 6.1).

The service launches a server-side Chromium, opens the source's login page and streams the
viewport to the operator over a WebSocket using the Chrome DevTools screencast. Mouse and
keyboard events from the operator are replayed into the page. The operator types credentials
and completes any verification; the service never sees, stores or echoes the password. When
the operator confirms, the service checks the page is authenticated, exports cookies and
localStorage as Playwright storage state, encrypts it and stores it in the chosen slot.

Credentials therefore never appear in the prompt, the environment, the database or the logs.
CAPTCHA / verification pages are never solved by code — they are shown to the human.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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
        asyncio.create_task(self._ack(params.get("sessionId")))

    async def _ack(self, session_id) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    async def next_frame(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        try:
            return await asyncio.wait_for(self.frames.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

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
        elif kind == "navigate":
            from urllib.parse import urlsplit

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
                  const label = a.getAttribute('placeholder') || a.getAttribute('aria-label') || a.getAttribute('name') || a.id || '';
                  const editable = ['INPUT','TEXTAREA'].includes(a.tagName) && !['checkbox','radio','submit','button','hidden','file'].includes(t) || a.isContentEditable;
                  return {tag: a.tagName.toLowerCase(), input_type: t, label: String(label).slice(0, 60), editable: !!editable}; }"""
            )
        except Exception as exc:  # page navigating, frame detached, ...
            return {"tag": None, "error": str(exc)[:80]}

    async def is_authenticated(self) -> Dict[str, Any]:
        html = await self._page.content()
        verdict = classify_response(200, html, self._page.url)
        low = html.lower()
        has_password = 'type="password"' in low or "type='password'" in low
        has_logout = "logout" in low or "log off" in low or "sign out" in low
        ok = verdict.kind == "ok" and (has_logout or not has_password)
        return {"authenticated": ok, "verdict": verdict.kind, "detail": verdict.detail, "url": self._page.url}

    async def export_storage_state(self) -> Dict[str, Any]:
        return await self._context.storage_state()

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
                    await r
            except Exception:
                pass


class LoginSessionRegistry:
    """One live human-login session per source per process."""

    def __init__(self):
        self._sessions: Dict[str, LoginSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, source_name: str, slot_number: int, login_url: str, started_by: str = "operator", viewport: Optional[Dict[str, int]] = None) -> LoginSession:
        """Open a browser for the human login. If one is already open for this source and slot (the
        operator reloaded the dashboard or lost the connection), it is reused rather than refused; a
        different slot replaces the open one."""
        async with self._lock:
            existing = self._sessions.get(source_name)
            if existing is not None and existing.status != "closed":
                if existing.slot_number == slot_number:
                    existing.status = "awaiting_human"
                    return existing
                await existing.close()
                self._sessions.pop(source_name, None)
            sess = LoginSession(source_name=source_name, slot_number=slot_number, login_url=login_url, started_by=started_by)
            if viewport:
                w = max(320, min(1920, int(viewport.get("width", 1280))))
                h = max(480, min(1600, int(viewport.get("height", 800))))
                sess.viewport = {"width": w, "height": h}
            await sess.start()
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
