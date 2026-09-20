"""
Login-session management for PakistanLawSite (Amendment §9; Cursor command §2, §5).

* Two continuity slots hold Fernet-encrypted Playwright storage state captured by a HUMAN login.
* Slot states: EMPTY → ACTIVE ↔ NEEDS_HUMAN_LOGIN / PAUSED / HALTED.
* Exactly one scraping session against the login source at a time (Redis lock; a second
  worker is refused).
* Ordinary disconnect: wait RECONNECT_SECONDS, reconnect the SAME slot, resume the SAME cursor;
  only if that fails may the alternate slot continue the same cursor. Never restart from page one.
* Verification / login expiry: slot → NEEDS_HUMAN_LOGIN, notify, continue with another valid slot
  or PAUSE the source. Nothing here solves a CAPTCHA.
* Explicit block: HALT the source, notify, no slot switch, no proxy, no stealth, admin review.
* No automatic recovery to the primary slot: once the alternate slot is in use it stays until a human acts.

The `Browser` protocol lets the pipeline run against real Playwright in production and a
scripted fake in tests; both raise the same exceptions.
"""

from __future__ import annotations

import asyncio
import html
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.models import BrowserSessionSlot, ScraperSource
from scraper.notify import notify
from scraper.security import ExplicitBlock, VerificationRequired, classify_response

logger = logging.getLogger(__name__)

LOCK_KEY = "corpus:login_session_lock:{source}"
LOCK_TTL_SECONDS = 3600


class LoginRequired(RuntimeError):
    """The session is no longer authenticated (login page / 401)."""


class BrowserDisconnected(RuntimeError):
    """Ordinary transport/browser failure — eligible for the 30-second same-slot reconnect."""


class NoActiveSlot(RuntimeError):
    pass


class SessionLockHeld(RuntimeError):
    """Another worker already holds the single login-session lock."""


@dataclass
class PageResult:
    url: str
    html: str
    status: int = 200
    content_type: str = "text/html"
    pdf_bytes: Optional[bytes] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def classify(self):
        return classify_response(self.status, self.html, self.url)


class Browser(Protocol):
    """What the pipeline needs from a browser bound to one slot's storage state."""

    slot_number: int

    async def goto(self, url: str) -> PageResult: ...

    async def submit_search(self, search_map: Dict[str, Any], values: Dict[str, str]) -> PageResult: ...

    async def download(self, url: str) -> bytes: ...

    async def close(self) -> None: ...


def raise_for_verdict(page: PageResult) -> None:
    """Translate a page classification into the pipeline's exceptions."""
    v = page.classify()
    if v.kind == "block":
        raise ExplicitBlock(v.kind, v.detail)
    if v.kind == "verification":
        raise VerificationRequired(v.detail)
    if v.kind in ("login", "multilogin"):
        raise LoginRequired(v.detail)


# --------------------------------------------------------------------------- lock
class SessionLock:
    def __init__(self, source_name: str, redis_client=None):
        self.key = LOCK_KEY.format(source=source_name)
        self._redis = redis_client
        self._token = hashlib.sha256(f"{source_name}{datetime.now(timezone.utc).timestamp()}".encode()).hexdigest()
        self._held = False

    async def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(settings.REDIS_URL)
        return self._redis

    async def acquire(self) -> None:
        r = await self._client()
        ok = await r.set(self.key, self._token, nx=True, ex=LOCK_TTL_SECONDS)
        if not ok:
            raise SessionLockHeld(f"login-session lock {self.key} is held by another worker")
        self._held = True

    async def refresh(self) -> None:
        if self._held:
            r = await self._client()
            ok = await r.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                1,
                self.key,
                self._token,
                str(LOCK_TTL_SECONDS),
            )
            if int(ok or 0) != 1:
                self._held = False
                raise SessionLockHeld(f"login-session lock {self.key} is no longer held by this worker")

    async def release(self) -> None:
        if not self._held:
            return
        r = await self._client()
        val = await r.get(self.key)
        if val is not None and (val.decode() if isinstance(val, bytes) else val) == self._token:
            await r.delete(self.key)
        self._held = False

    async def __aenter__(self) -> "SessionLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()


# --------------------------------------------------------------------------- slot manager
class SessionManager:
    def __init__(self, db: AsyncSession, source: ScraperSource):
        self.db = db
        self.source = source

    async def slots(self) -> List[BrowserSessionSlot]:
        rows = (await self.db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == self.source.source_name).order_by(BrowserSessionSlot.slot_number))).scalars().all()
        if not rows:
            for n in (1, 2):
                self.db.add(BrowserSessionSlot(source_name=self.source.source_name, slot_number=n, role="primary" if n == 1 else "alternate", state="EMPTY"))
            await self.db.flush()
            rows = (await self.db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == self.source.source_name).order_by(BrowserSessionSlot.slot_number))).scalars().all()
        return list(rows)

    async def slot(self, number: int) -> BrowserSessionSlot:
        for s in await self.slots():
            if s.slot_number == number:
                return s
        raise NoActiveSlot(f"slot {number} does not exist")

    async def current_slot(self) -> Optional[BrowserSessionSlot]:
        """The slot in use. Sticky: never auto-recovers to the primary slot."""
        cfg = dict(self.source.config_json or {})
        wanted = cfg.get("current_slot")
        slots = await self.slots()
        if wanted:
            for s in slots:
                if s.slot_number == wanted and s.state == "ACTIVE":
                    return s
        for s in slots:
            if s.state == "ACTIVE":
                cfg["current_slot"] = s.slot_number
                self.source.config_json = cfg
                return s
        return None

    async def alternate_active_slot(self, exclude: int) -> Optional[BrowserSessionSlot]:
        for s in await self.slots():
            if s.slot_number != exclude and s.state == "ACTIVE":
                return s
        return None

    async def switch_current(self, slot_number: int, reason: str) -> None:
        cfg = dict(self.source.config_json or {})
        cfg["current_slot"] = slot_number
        cfg["current_slot_reason"] = reason
        self.source.config_json = cfg
        await self.db.flush()

    # ---------------------------------------------------------------- storage state
    async def save_storage_state(self, slot_number: int, storage_state: Dict[str, Any], *, by: str = "human") -> BrowserSessionSlot:
        s = await self.slot(slot_number)
        raw = json.dumps(storage_state, separators=(",", ":"))
        s.storage_state_encrypted = settings.encrypt_value(raw)
        s.storage_state_hash = hashlib.sha256(raw.encode()).hexdigest()
        s.state = "ACTIVE"
        s.state_reason = "human login completed"
        s.logged_in_by = by
        s.logged_in_at = datetime.now(timezone.utc)
        s.last_verified_at = s.logged_in_at
        if self.source.state == "PAUSED":
            self.source.state = "ACTIVE"
            self.source.state_reason = None
            self.source.state_changed_at = datetime.now(timezone.utc)
        await self.db.flush()
        await notify(self.db, level="info", code="SLOT_ACTIVE", message=f"slot {slot_number} active after human login", source_name=self.source.source_name)
        return s

    def load_storage_state(self, slot: BrowserSessionSlot) -> Dict[str, Any]:
        if not slot.storage_state_encrypted:
            raise NoActiveSlot(f"slot {slot.slot_number} has no storage state")
        return json.loads(settings.decrypt_value(slot.storage_state_encrypted))

    async def save_login_credentials(self, slot_number: int, username: str, password: str, *, by: str = "operator") -> BrowserSessionSlot:
        s = await self.slot(slot_number)
        now = datetime.now(timezone.utc)
        s.login_username_encrypted = settings.encrypt_value(username.strip())
        s.login_password_encrypted = settings.encrypt_value(password)
        s.login_credentials_updated_at = now
        s.login_credentials_updated_by = by
        await self.db.flush()
        return s

    def load_login_credentials(self, slot: BrowserSessionSlot) -> Optional[Dict[str, str]]:
        if not slot.login_username_encrypted or not slot.login_password_encrypted:
            return None
        try:
            username = settings.decrypt_value(slot.login_username_encrypted).strip()
            password = settings.decrypt_value(slot.login_password_encrypted)
        except Exception as exc:
            logger.warning(
                "Ignoring malformed saved credentials for %s slot %s: %s",
                self.source.source_name,
                slot.slot_number,
                exc,
            )
            return None
        if not username or not password:
            return None
        return {"username": username, "password": password}

    async def clear_login_credentials(self, slot_number: int) -> BrowserSessionSlot:
        s = await self.slot(slot_number)
        s.login_username_encrypted = None
        s.login_password_encrypted = None
        s.login_credentials_updated_at = None
        s.login_credentials_updated_by = None
        await self.db.flush()
        return s

    # ---------------------------------------------------------------- state transitions
    async def mark_needs_human_login(self, slot_number: int, reason: str) -> None:
        s = await self.slot(slot_number)
        s.state = "NEEDS_HUMAN_LOGIN"
        s.state_reason = reason[:1000]
        await self.db.flush()
        await notify(self.db, level="warning", code="NEEDS_HUMAN_LOGIN", message=f"slot {slot_number}: {reason}", source_name=self.source.source_name)
        if await self.alternate_active_slot(slot_number) is None:
            await self.pause_source(f"no valid slot; slot {slot_number}: {reason}")

    async def pause_source(self, reason: str) -> None:
        self.source.state = "PAUSED"
        self.source.state_reason = reason[:1000]
        self.source.state_changed_at = datetime.now(timezone.utc)
        await self.db.flush()
        await notify(self.db, level="warning", code="SOURCE_PAUSED", message=reason, source_name=self.source.source_name)

    async def halt_source(self, reason: str, slot_number: Optional[int] = None) -> None:
        """Explicit block. No slot switch, no managed fetch, no proxy/stealth. Admin must re-enable."""
        self.source.state = "HALTED"
        self.source.state_reason = reason[:1000]
        self.source.state_changed_at = datetime.now(timezone.utc)
        self.source.requires_admin_review = True
        if slot_number is not None:
            s = await self.slot(slot_number)
            s.state = "HALTED"
            s.state_reason = reason[:1000]
            s.halted_at = datetime.now(timezone.utc)
        await self.db.flush()
        await notify(self.db, level="critical", code="SOURCE_HALTED", message=f"explicit block — {reason}. No bypass attempted; admin review required.", source_name=self.source.source_name)

    async def touch(self, slot_number: int) -> None:
        s = await self.slot(slot_number)
        s.last_used_at = datetime.now(timezone.utc)
        s.last_verified_at = s.last_used_at
        await self.db.flush()


# --------------------------------------------------------------------------- Playwright browser
class PlaywrightBrowser:
    """Headless Chromium bound to one slot's storage state. Ordinary search-page fields are
    automated normally; verification pages are never solved."""

    def __init__(self, storage_state: Dict[str, Any], slot_number: int, *, base_url: str, headless: Optional[bool] = None):
        self.slot_number = slot_number
        self._storage_state = storage_state
        self.base_url = base_url
        self._headless = settings.PLAYWRIGHT_HEADLESS if headless is None else headless
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    async def start(self) -> "PlaywrightBrowser":
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless, executable_path=settings.PLAYWRIGHT_EXECUTABLE_PATH or None)
        self._context = await self._browser.new_context(storage_state=self._storage_state, user_agent=None)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT_MS)
        self._page.set_default_navigation_timeout(settings.PLAYWRIGHT_TIMEOUT_MS)
        return self

    async def _wrap(self, coro):
        from playwright.async_api import Error as PWError

        try:
            return await coro
        except PWError as exc:
            msg = str(exc)
            if any(k in msg for k in ("Target closed", "Browser has been closed", "net::ERR_", "Navigation failed", "Timeout", "disconnected", "Connection closed")):
                raise BrowserDisconnected(msg) from exc
            raise

    @staticmethod
    def _header_int(resp, name: str) -> Optional[int]:
        if resp is None:
            return None
        raw = resp.headers.get(name) or resp.headers.get(name.lower())
        if raw in (None, ""):
            return None
        try:
            return int(str(raw).strip())
        except Exception:
            return None

    async def _dom_shape(self) -> Dict[str, Any]:
        return await self._wrap(
            self._page.evaluate(
                """() => {
                    const body = document.body;
                    const grid = document.querySelector('#archivedpatientGrid');
                    const tbody = grid && grid.tBodies && grid.tBodies.length ? grid.tBodies[0] : null;
                    return {
                        forms: document.forms ? document.forms.length : 0,
                        inputs: document.querySelectorAll('input').length,
                        has_archivedpatient_grid: Boolean(grid),
                        archivedpatient_rows: tbody ? tbody.rows.length : 0,
                        has_logout: Boolean(document.querySelector('a[href*="logout" i], a[href*="logoff" i]')),
                        body_preview: (body && body.textContent ? body.textContent : '').slice(0, 2500),
                    };
                }"""
            )
        )

    async def _has_archivedpatient_grid(self) -> bool:
        return bool(
            await self._wrap(
                self._page.evaluate("""() => Boolean(document.querySelector('#archivedpatientGrid'))""")
            )
        )

    async def _capture_archived_grid_snapshot(self) -> Optional[Dict[str, Any]]:
        return await self._wrap(
            self._page.evaluate(
                """(maxRows) => {
                    const table = document.querySelector('#archivedpatientGrid');
                    if (!table) return null;
                    const headRow = table.tHead && table.tHead.rows.length ? table.tHead.rows[0] : null;
                    const headers = headRow ? Array.from(headRow.cells).map((th) => (th.textContent || '').trim()) : [];
                    const normalizedHeaders = headers.map((h) => h.toLowerCase().replace(/\\s+/g, ' ').trim());
                    const pickIndex = (hints, fallbackIndex) => {
                        for (let i = 0; i < normalizedHeaders.length; i += 1) {
                            const header = normalizedHeaders[i];
                            if (hints.some((hint) => header.includes(hint))) {
                                return i;
                            }
                        }
                        return fallbackIndex;
                    };
                    const nonReadIndexes = normalizedHeaders
                        .map((h, idx) => ({ h, idx }))
                        .filter((entry) => !entry.h.includes('read'))
                        .map((entry) => entry.idx);
                    const citationIdx = pickIndex(['citation'], nonReadIndexes[0] ?? 0);
                    const titleIdx = pickIndex(['title', 'party'], nonReadIndexes[1] ?? 1);
                    const courtIdx = pickIndex(['court'], nonReadIndexes[2] ?? 2);
                    const cellAt = (cells, idx) => (idx >= 0 && idx < cells.length ? (cells[idx] || '') : '');
                    const rows = [];
                    const bodyRows = table.tBodies && table.tBodies.length ? Array.from(table.tBodies[0].rows) : [];
                    const trNodes = bodyRows.slice(0, maxRows);
                    for (const tr of trNodes) {
                        const cells = Array.from(tr.cells || []).map((td) => (td.textContent || '').trim());
                        const anchors = Array.from(tr.querySelectorAll('a[href]'));
                        let detailUrl = null;
                        let pdfUrl = null;
                        for (const a of anchors) {
                            const href = a.href || '';
                            if (!href) continue;
                            if (!pdfUrl && href.toLowerCase().endsWith('.pdf')) {
                                pdfUrl = href;
                            } else if (!detailUrl) {
                                detailUrl = href;
                            }
                        }
                        if (!detailUrl) {
                            const readControl = tr.querySelector('input.courtWiseSearchBtn[casetypeid], .courtWiseSearchBtn[casetypeid], [casetypeid]');
                            let caseTypeId = (readControl && readControl.getAttribute('casetypeid')) ? readControl.getAttribute('casetypeid').trim() : '';
                            if (!caseTypeId) {
                                const readCell = tr.cells && tr.cells.length ? tr.cells[tr.cells.length - 1] : tr;
                                const idMatch = ((readCell && readCell.innerHTML) || '').match(/casetypeid\\s*=\\s*['"]?([^'"\\s>]+)/i);
                                if (idMatch && idMatch[1]) {
                                    caseTypeId = idMatch[1].trim();
                                }
                            }
                            if (caseTypeId) {
                                detailUrl = `${window.location.origin}/Login/ReferenceCaseLawSearch?CaseName=${encodeURIComponent(caseTypeId)}&&court= &&Row=0 &&bookName=undefined`;
                            }
                        }
                        rows.push({
                            citation: cellAt(cells, citationIdx),
                            title: cellAt(cells, titleIdx),
                            court: cellAt(cells, courtIdx),
                            detail_url: detailUrl,
                            pdf_url: pdfUrl,
                        });
                    }
                    const nextLink = document.querySelector('#archivedpatientGrid_next a, .dataTables_paginate a.next, a[rel="next"]');
                    const nextDisabled = nextLink ? (nextLink.classList.contains('disabled') || nextLink.parentElement?.classList.contains('disabled')) : true;
                    return {
                        headers,
                        rows,
                        next_url: !nextDisabled && nextLink && nextLink.href ? nextLink.href : null,
                        body_preview: (document.body && document.body.textContent ? document.body.textContent : '').slice(0, 2500),
                        has_logout: Boolean(document.querySelector('a[href*="logout" i], a[href*="logoff" i]')),
                    };
                }""",
                int(settings.PLS_ARCHIVED_GRID_MAX_ROWS),
            )
        )

    @staticmethod
    def _render_compact_archived_grid_html(snapshot: Dict[str, Any]) -> str:
        headers = snapshot.get("headers") or ["Citation", "Title", "Court", "Read"]
        rows = snapshot.get("rows") or []
        parts: List[str] = ["<html><body>"]
        if snapshot.get("has_logout"):
            parts.append("<a href=\"/logout\">Logout</a>")
        parts.append("<table id=\"archivedpatientGrid\"><thead><tr>")
        for h in headers:
            parts.append(f"<th>{html.escape(str(h))}</th>")
        parts.append("</tr></thead><tbody>")
        for row in rows:
            parts.append("<tr>")
            parts.append(f"<td>{html.escape(str(row.get('citation') or ''))}</td>")
            parts.append(f"<td>{html.escape(str(row.get('title') or ''))}</td>")
            parts.append(f"<td>{html.escape(str(row.get('court') or ''))}</td>")
            href = row.get("detail_url") or row.get("pdf_url")
            if href:
                parts.append(f"<td><a href=\"{html.escape(str(href), quote=True)}\">Read</a></td>")
            else:
                parts.append("<td></td>")
            parts.append("</tr>")
        parts.append("</tbody></table>")
        if snapshot.get("next_url"):
            parts.append(f"<a rel=\"next\" href=\"{html.escape(str(snapshot['next_url']), quote=True)}\">Next</a>")
        preview = snapshot.get("body_preview") or ""
        if preview:
            parts.append(f"<div id=\"guard_preview\">{html.escape(str(preview))}</div>")
        parts.append("</body></html>")
        return "".join(parts)

    @staticmethod
    def _render_oversize_stub(dom: Dict[str, Any], *, content_length: Optional[int]) -> str:
        preview = dom.get("body_preview") or ""
        fields = [
            ("inputs", dom.get("inputs", 0)),
            ("forms", dom.get("forms", 0)),
            ("content_length", content_length if content_length is not None else "unknown"),
        ]
        attrs = " ".join(f"data-{k}=\"{html.escape(str(v), quote=True)}\"" for k, v in fields)
        return (
            f"<html><body><div id=\"oversize_guard\" {attrs}>"
            "oversized page skipped by Playwright guard"
            "</div>"
            f"<div id=\"guard_preview\">{html.escape(str(preview))}</div>"
            "</body></html>"
        )

    async def _capture_html(self, *, resp=None) -> tuple[str, Dict[str, Any]]:
        content_length = self._header_int(resp, "content-length")
        if await self._has_archivedpatient_grid():
            snapshot = await self._capture_archived_grid_snapshot()
            if not snapshot:
                snapshot = {"headers": [], "rows": [], "next_url": None, "body_preview": "", "has_logout": False}
            html_compact = self._render_compact_archived_grid_html(snapshot)
            return html_compact, {
                "content_guard": "archivedpatientGrid_compact",
                "content_length": content_length,
                "rows": len(snapshot.get("rows") or []),
                "row_cap": int(settings.PLS_ARCHIVED_GRID_MAX_ROWS),
            }
        dom = await self._dom_shape()
        oversized = False
        if content_length is not None and content_length >= settings.PLAYWRIGHT_MAX_HTML_BYTES:
            oversized = True
        if int(dom.get("inputs") or 0) >= settings.PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD:
            oversized = True
        if oversized:
            logger.warning(
                "oversized HTML guard tripped for slot=%s url=%s inputs=%s content_length=%s",
                self.slot_number,
                self._page.url,
                dom.get("inputs"),
                content_length,
            )
            stub = self._render_oversize_stub(dom, content_length=content_length)
            return stub, {
                "content_guard": "oversize_stub",
                "inputs": int(dom.get("inputs") or 0),
                "forms": int(dom.get("forms") or 0),
                "content_length": content_length,
            }
        return await self._wrap(self._page.content()), {}

    async def goto(self, url: str) -> PageResult:
        resp = await self._wrap(
            self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.PLAYWRIGHT_TIMEOUT_MS,
            )
        )
        html_text, metadata = await self._capture_html(resp=resp)
        status = resp.status if resp else 200
        ctype = (resp.headers.get("content-type", "") if resp else "")
        return PageResult(url=self._page.url, html=html_text, status=status, content_type=ctype, metadata=metadata)

    async def _wait_for_post_submit_navigation(self, trigger) -> None:
        from playwright.async_api import TimeoutError as PWTimeoutError

        try:
            async with self._page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=settings.PLAYWRIGHT_TIMEOUT_MS,
            ):
                await self._wrap(trigger())
        except PWTimeoutError:
            # Some search surfaces update in-place without a full document navigation.
            return

    async def submit_search(self, search_map: Dict[str, Any], values: Dict[str, str]) -> PageResult:
        fields = search_map.get("fields") or {}
        for role, value in values.items():
            f = fields.get(role)
            if not f:
                continue
            sel, kind = f["selector"], f.get("kind", "text")
            if kind == "select":
                await self._wrap(self._page.select_option(sel, value=value))
            elif kind == "checkbox":
                if value in ("1", "true", "on"):
                    await self._wrap(self._page.check(sel))
                else:
                    await self._wrap(self._page.uncheck(sel))
            else:
                await self._wrap(self._page.fill(sel, value))
        submit = fields.get("submit")
        if submit:
            await self._wait_for_post_submit_navigation(
                lambda: self._page.click(submit["selector"])
            )
        else:
            await self._wait_for_post_submit_navigation(
                lambda: self._page.keyboard.press("Enter")
            )
        html_text, metadata = await self._capture_html(resp=None)
        return PageResult(url=self._page.url, html=html_text, status=200, metadata=metadata)

    async def download(self, url: str) -> bytes:
        resp = await self._wrap(self._context.request.get(url))
        if resp.status >= 400:
            raise BrowserDisconnected(f"download HTTP {resp.status}") if resp.status >= 500 else ExplicitBlock("block", f"HTTP {resp.status} on download")
        return await self._wrap(resp.body())

    async def storage_state(self) -> Dict[str, Any]:
        return await self._context.storage_state()

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass


class BrowserFactory(Protocol):
    async def __call__(self, storage_state: Dict[str, Any], slot_number: int) -> Browser: ...


async def playwright_browser_factory(storage_state: Dict[str, Any], slot_number: int) -> Browser:
    b = PlaywrightBrowser(storage_state, slot_number, base_url=settings.PLS_BASE_URL)
    return await b.start()


# --------------------------------------------------------------------------- continuity runner
@dataclass
class ContinuityRunner:
    """Runs operations against the current slot with the mandated recovery ladder."""

    manager: SessionManager
    factory: Any
    reconnect_seconds: int = field(default_factory=lambda: settings.RECONNECT_SECONDS)
    sleep: Any = asyncio.sleep
    browser: Optional[Browser] = None
    reconnects: int = 0

    async def open(self, slot: BrowserSessionSlot) -> Browser:
        state = self.manager.load_storage_state(slot)
        self.browser = await self.factory(state, slot.slot_number)
        return self.browser

    async def ensure_browser(self) -> Browser:
        if self.browser is not None:
            return self.browser
        slot = await self.manager.current_slot()
        if slot is None:
            raise NoActiveSlot("no ACTIVE slot")
        return await self.open(slot)

    async def close(self) -> None:
        if self.browser is not None:
            try:
                await self.browser.close()
            finally:
                self.browser = None

    async def run(self, op):
        """op(browser) -> result. Same cursor is the caller's responsibility: the op is re-invoked
        unchanged, so whatever cursor it closed over is the cursor it resumes."""
        browser = await self.ensure_browser()
        slot_no = browser.slot_number
        try:
            result = await op(browser)
            await self.manager.touch(slot_no)
            return result
        except BrowserDisconnected as exc:
            logger.warning("slot %s disconnected: %s; waiting %ss then reconnecting SAME slot", slot_no, exc, self.reconnect_seconds)
            await self.close()
            await self.sleep(self.reconnect_seconds)
            slot = await self.manager.slot(slot_no)
            try:
                if slot.state != "ACTIVE":
                    raise BrowserDisconnected(f"slot {slot_no} no longer ACTIVE")
                browser = await self.open(slot)
                result = await op(browser)
                slot.reconnect_count += 1
                self.reconnects += 1
                await self.manager.touch(slot_no)
                return result
            except BrowserDisconnected as exc2:
                logger.warning("same-slot reconnect failed for slot %s: %s", slot_no, exc2)
                await self.close()
                alt = await self.manager.alternate_active_slot(slot_no)
                if alt is None:
                    await self.manager.pause_source(f"slot {slot_no} unreachable after reconnect and no alternate slot")
                    raise
                await self.manager.switch_current(alt.slot_number, f"continuity recovery from slot {slot_no}")
                browser = await self.open(alt)
                result = await op(browser)
                self.reconnects += 1
                await self.manager.touch(alt.slot_number)
                return result
        except (LoginRequired, VerificationRequired) as exc:
            await self.close()
            await self.manager.mark_needs_human_login(slot_no, f"{type(exc).__name__}: {exc}")
            alt = await self.manager.alternate_active_slot(slot_no)
            if alt is None:
                raise
            await self.manager.switch_current(alt.slot_number, f"slot {slot_no} needs human login")
            browser = await self.open(alt)
            result = await op(browser)
            await self.manager.touch(alt.slot_number)
            return result
        except ExplicitBlock as exc:
            await self.close()
            await self.manager.halt_source(str(exc), slot_number=slot_no)
            raise
