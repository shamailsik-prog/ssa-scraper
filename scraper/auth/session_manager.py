"""
Login-session management for PakistanLawSite (Amendment §9; Cursor command §2, §5).

* Two continuity slots hold Fernet-encrypted Playwright storage state captured by a HUMAN login.
* Slot states: EMPTY → ACTIVE ↔ NEEDS_HUMAN_LOGIN / PAUSED / HALTED.
* Login-session lock: exclusive by default; two holders only when harvest concurrency is 2.
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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from scraper.config import settings
from scraper.models import BrowserSessionSlot, ScraperSource
from scraper.notify import notify
from scraper.security import ExplicitBlock, VerificationRequired, classify_response, scrub_secrets

logger = logging.getLogger(__name__)

ARCHIVED_GRID_SEEK_JS = (Path(__file__).resolve().parent / "archived_grid_seek.js").read_text(encoding="utf-8")

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

    async def goto(self, url: str, **kwargs: Any) -> PageResult: ...

    async def submit_search(self, search_map: Dict[str, Any], values: Dict[str, str]) -> PageResult: ...

    async def download(self, url: str) -> bytes: ...

    async def close(self) -> None: ...


def raise_for_verdict(page: PageResult) -> None:
    """Translate a page classification into the pipeline's exceptions."""
    v = page.classify()
    if v.kind == "block":
        raise ExplicitBlock(v.kind, v.detail)
    where = f" (landed on {safe_url_for_record(page.url)})" if page.url else ""
    if v.kind == "verification":
        raise VerificationRequired(f"{v.detail}{where}")
    if v.kind in ("login", "multilogin"):
        raise LoginRequired(f"{v.detail}{where}")


def safe_url_for_record(url: Optional[str]) -> str:
    """Scheme, host and path of a URL only: the query string and fragment (where return-URL tokens,
    session ids or credential-shaped parameters would sit) are never persisted or logged."""
    if not url:
        return ""
    try:
        parts = urlsplit(str(url))
    except Exception:
        return "[unparseable url]"
    if not parts.scheme or not parts.netloc:
        return scrub_secrets(str(url).split("?", 1)[0])[:300]
    return scrub_secrets(f"{parts.scheme}://{parts.netloc}{parts.path or '/'}")[:300]


def cookie_summary(storage_state: Optional[Dict[str, Any]]) -> List[str]:
    """Names, domains and expiry times of the cookies in a storage state: never their values.
    Logged when a slot is opened or refreshed so a lost login can be traced to an expired cookie."""
    out: List[str] = []
    for c in (storage_state or {}).get("cookies") or []:
        if not isinstance(c, dict):
            continue
        exp = c.get("expires")
        try:
            exp_txt = "session" if exp in (None, -1, "-1") else datetime.fromtimestamp(float(exp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        except Exception:
            exp_txt = str(exp)
        out.append(f"{c.get('name')}@{c.get('domain')} exp={exp_txt}")
    return out


# --------------------------------------------------------------------------- lock
class SessionLock:
    def __init__(self, source_name: str, redis_client=None, *, max_holders: int = 1):
        holders = 1 if int(max_holders or 1) <= 1 else 2
        self.max_holders = holders
        self.exclusive_key = LOCK_KEY.format(source=source_name)
        self.key = self.exclusive_key if holders == 1 else f"{self.exclusive_key}:holders"
        self._redis = redis_client
        self._own_client = redis_client is None
        self._token = hashlib.sha256(f"{source_name}{datetime.now(timezone.utc).timestamp()}".encode()).hexdigest()
        self._held = False

    async def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(settings.REDIS_URL)
        return self._redis

    async def acquire(self) -> None:
        r = await self._client()
        if await r.get(self.exclusive_key) and self.max_holders > 1:
            raise SessionLockHeld(f"login-session lock {self.exclusive_key} is held by another worker")
        if self.max_holders == 1:
            shared = int(await r.scard(f"{self.exclusive_key}:holders") or 0)
            if shared > 0:
                raise SessionLockHeld(f"login-session lock {self.exclusive_key}:holders is held by another worker")
            ok = await r.set(self.key, self._token, nx=True, ex=LOCK_TTL_SECONDS)
        else:
            ok = await r.eval(
                "if redis.call('scard', KEYS[1]) < tonumber(ARGV[2]) then redis.call('sadd', KEYS[1], ARGV[1]); redis.call('expire', KEYS[1], ARGV[3]); return 1 else return 0 end",
                1,
                self.key,
                self._token,
                str(self.max_holders),
                str(LOCK_TTL_SECONDS),
            )
        if not ok:
            raise SessionLockHeld(f"login-session lock {self.key} is held by another worker")
        self._held = True

    async def refresh(self) -> None:
        if self._held:
            r = await self._client()
            if self.max_holders == 1:
                ok = await r.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                    1,
                    self.key,
                    self._token,
                    str(LOCK_TTL_SECONDS),
                )
            else:
                ok = await r.eval(
                    "if redis.call('sismember', KEYS[1], ARGV[1]) == 1 then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                    1,
                    self.key,
                    self._token,
                    str(LOCK_TTL_SECONDS),
                )
            if int(ok or 0) != 1:
                self._held = False
                raise SessionLockHeld(f"login-session lock {self.key} is no longer held by this worker")

    async def release(self) -> None:
        try:
            if not self._held:
                return
            r = await self._client()
            if self.max_holders == 1:
                val = await r.get(self.key)
                if val is not None and (val.decode() if isinstance(val, bytes) else val) == self._token:
                    await r.delete(self.key)
            else:
                await r.srem(self.key, self._token)
                if int(await r.scard(self.key) or 0) == 0:
                    await r.delete(self.key)
            self._held = False
        finally:
            if self._own_client and self._redis is not None:
                try:
                    await self._redis.aclose()
                except Exception:
                    pass
                self._redis = None

    async def __aenter__(self) -> "SessionLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()


# --------------------------------------------------------------------------- source config
async def merge_source_config(db: AsyncSession, source: ScraperSource, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge top-level keys into scraper_sources.config_json atomically (JSONB `||`).

    Two reporter shards run at the same time in two worker processes, each holding its own copy of
    the source row; writing the whole JSON back from either copy would overwrite the other shard's
    cursor and pacing counters. Each writer therefore sends only its own keys, and the in-memory
    row is set to the merged value the database returns."""
    if not patch:
        return dict(source.config_json or {})
    stmt = (
        update(ScraperSource)
        .where(ScraperSource.id == source.id)
        .values(config_json=ScraperSource.config_json.op("||")(patch))
        .returning(ScraperSource.config_json)
    )
    merged = (await db.execute(stmt)).scalar_one()
    set_committed_value(source, "config_json", dict(merged or {}))
    return dict(merged or {})


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
                await merge_source_config(self.db, self.source, {"current_slot": s.slot_number})
                return s
        return None

    async def alternate_active_slot(self, exclude: int) -> Optional[BrowserSessionSlot]:
        for s in await self.slots():
            if s.slot_number != exclude and s.state == "ACTIVE":
                return s
        return None

    async def switch_current(self, slot_number: int, reason: str) -> None:
        await merge_source_config(self.db, self.source, {"current_slot": slot_number, "current_slot_reason": reason})
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
        await self._resume_source_if_paused()
        await self.db.flush()
        await notify(self.db, level="info", code="SLOT_ACTIVE", message=f"slot {slot_number} active (login by {by})", source_name=self.source.source_name)
        return s

    async def _resume_source_if_paused(self) -> None:
        if self.source.state == "PAUSED":
            self.source.state = "ACTIVE"
            self.source.state_reason = None
            self.source.state_changed_at = datetime.now(timezone.utc)
            self.source.next_scrape_at = datetime.now(timezone.utc)

    async def reactivate_slot(self, slot_number: int, reason: str, *, by: str = "recovery") -> BrowserSessionSlot:
        """A slot the site had bounced turns out to hold a live session after a cool-down (the site
        throttled the account rather than ending the login): put it back into rotation."""
        s = await self.slot(slot_number)
        s.state = "ACTIVE"
        s.state_reason = reason[:1000]
        s.last_verified_at = datetime.now(timezone.utc)
        await self._resume_source_if_paused()
        await self.db.flush()
        await notify(self.db, level="info", code="SLOT_ACTIVE", message=f"slot {slot_number} active again: {reason} ({by})", source_name=self.source.source_name)
        return s

    def load_storage_state(self, slot: BrowserSessionSlot) -> Dict[str, Any]:
        if not slot.storage_state_encrypted:
            raise NoActiveSlot(f"slot {slot.slot_number} has no storage state")
        return json.loads(settings.decrypt_value(slot.storage_state_encrypted))

    async def refresh_storage_state(
        self,
        slot_number: int,
        storage_state: Dict[str, Any],
        *,
        expected_hash: Optional[str] = None,
    ) -> Optional[str]:
        """Keep an ACTIVE slot's stored session current with the cookies the site has renewed during
        a run. The login itself is still the human's; only its live continuation is stored, so the next
        job opens with the renewed cookies instead of the ones captured at login time.

        Compare-and-update: the row is written only while it is still ACTIVE and still holds the state
        this browser was opened with (`expected_hash`). A human login completed meanwhile, or an admin
        clear, changes that hash or state and therefore wins; the stale browser's cookies never
        overwrite it. Returns the new hash when the stored state changed, else None."""
        if not isinstance(storage_state, dict) or not storage_state.get("cookies"):
            return None
        raw = json.dumps(storage_state, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        guard = [
            BrowserSessionSlot.source_name == self.source.source_name,
            BrowserSessionSlot.slot_number == slot_number,
            BrowserSessionSlot.state == "ACTIVE",
            BrowserSessionSlot.storage_state_encrypted.isnot(None),
        ]
        if expected_hash is not None:
            guard.append(BrowserSessionSlot.storage_state_hash == expected_hash)
        if expected_hash is not None and digest == expected_hash:
            await self.db.execute(update(BrowserSessionSlot).where(*guard).values(last_verified_at=now))
            return None
        result = await self.db.execute(
            update(BrowserSessionSlot)
            .where(*guard, BrowserSessionSlot.storage_state_hash != digest)
            .values(storage_state_encrypted=settings.encrypt_value(raw), storage_state_hash=digest, last_verified_at=now)
        )
        changed = int(result.rowcount or 0) > 0
        # Refresh the in-memory row so later reads in this session see what the database holds.
        for row in await self.slots():
            if row.slot_number == slot_number:
                await self.db.refresh(row)
        if not changed:
            logger.info(
                "slot %s storage state not refreshed: the row changed meanwhile (new human login, admin clear or paused slot); the newer state is kept",
                slot_number,
            )
            return None
        logger.info("slot %s storage state refreshed from the live browser: %s", slot_number, ", ".join(cookie_summary(storage_state)) or "no cookies")
        return digest

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
        # Keep this evaluate cheap: never walk every DataTables row or serialize
        # multi-MB innerText/outerHTML here — that is what hung CitationSearch.
        return await self._wrap(
            self._page.evaluate(
                """() => {
                    const body = document.body;
                    const grid = document.getElementById('archivedpatientGrid');
                    let archivedpatient_rows = 0;
                    if (grid) {
                        const tbody = grid.tBodies && grid.tBodies[0];
                        archivedpatient_rows = tbody && tbody.rows ? tbody.rows.length : 0;
                    }
                    // innerText forces style + layout of the whole document: on the 20k-row
                    // CitationSearch grid that alone takes seconds. Build the preview from the
                    // chrome around the grid instead (textContent needs no layout).
                    let body_preview = '';
                    if (grid && archivedpatient_rows > 500) {
                        const parts = [];
                        const children = body ? body.children : [];
                        for (let i = 0; i < children.length && parts.join(' ').length < 400; i += 1) {
                            const el = children[i];
                            if (!el || el === grid || el.contains(grid) || el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
                            const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
                            if (t) parts.push(t);
                        }
                        body_preview = parts.join(' ').slice(0, 400);
                    } else {
                        body_preview = (body && body.innerText ? body.innerText : '').slice(0, 400);
                    }
                    return {
                        forms: document.forms ? document.forms.length : 0,
                        inputs: document.querySelectorAll('input').length,
                        has_archivedpatient_grid: Boolean(grid),
                        archivedpatient_rows,
                        has_logout: Boolean(document.querySelector('a[href*="logout" i], a[href*="logoff" i]')),
                        body_preview,
                    };
                }"""
            )
        )

    async def _capture_archived_grid_snapshot(self, *, start_row: int = 0, max_rows_override: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Compact row extract for archivedpatientGrid without page.content().

        Critical performance rules:
        - iterate tbody.rows up to maxRows (do NOT Array.from(querySelectorAll(...)))
        - never read tr.innerHTML (re-serializes huge row markup)
        - synthesize detail URLs from casetypeid when anchors are absent
        """
        max_rows = int(max_rows_override or settings.PLS_ARCHIVED_GRID_MAX_ROWS)
        safe_start_row = max(0, int(start_row or 0))
        logger.info(
            "archivedpatientGrid compact snapshot starting slot=%s max_rows=%s start_row=%s url=%s",
            self.slot_number,
            max_rows,
            safe_start_row,
            self._page.url if self._page else None,
        )
        started = datetime.now(timezone.utc)
        try:
            snapshot = await asyncio.wait_for(
                self._wrap(
                    self._page.evaluate(
                        "async ({ maxRows, startRow }) => {\n"
                        + ARCHIVED_GRID_SEEK_JS
                        + """
                    const table = document.getElementById('archivedpatientGrid');
                    if (!table) return null;
                    const requestedStartRow = Number.isFinite(Number(startRow)) ? Math.max(0, Math.floor(Number(startRow))) : 0;
                    let appliedStartRow = 0;
                    let totalRows = null;
                    let pageLength = null;
                    let seekMode = 'none';
                    try {
                        const seek = await seekArchivedGridAbsolute(table, startRow, maxRows);
                        if (seek) {
                            totalRows = seek.total_rows;
                            pageLength = seek.page_length;
                            seekMode = seek.seek_mode || 'none';
                            if (seekMode === 'datatable' || seekMode === 'dom_absolute') {
                                appliedStartRow = Number.isFinite(Number(seek.start_row)) ? Math.max(0, Math.floor(Number(seek.start_row))) : 0;
                            } else {
                                appliedStartRow = 0;
                            }
                        }
                    } catch (_seekError) {
                        // Keep compact snapshot resilient; live path is DOM slice, DT is optional.
                        appliedStartRow = 0;
                        seekMode = 'none';
                    }
                    if (seekMode !== 'datatable' && seekMode !== 'dom_absolute') {
                        appliedStartRow = 0;
                        const tbody = (table.tBodies && table.tBodies[0]) || table.querySelector('tbody');
                        const trCollection = tbody && tbody.rows ? tbody.rows : [];
                        // Only scroll a row that is actually in this DOM page. Do not map a
                        // deep start_row onto the last first-page row — that invents an offset.
                        const inDom = requestedStartRow < trCollection.length ? trCollection[requestedStartRow] : null;
                        if (inDom && inDom.scrollIntoView) {
                            try {
                                inDom.scrollIntoView({ block: 'nearest' });
                                if (seekMode === 'unavailable' || seekMode === 'none') {
                                    seekMode = 'scroll';
                                }
                            } catch (_scrollError) {
                                if (seekMode === 'unavailable' || seekMode === 'none') {
                                    seekMode = 'dom';
                                }
                            }
                        } else if (seekMode === 'unavailable' || seekMode === 'none') {
                            seekMode = 'dom';
                        }
                    }
                    const headers = Array.from(table.querySelectorAll('thead th')).map((th) => (th.textContent || '').trim());
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
                    const tbody = (table.tBodies && table.tBodies[0]) || table.querySelector('tbody');
                    const trCollection = tbody && tbody.rows ? tbody.rows : [];
                    // Live default: full <tr> list — slice [offset : offset+page_size].
                    // DataTables fallback already materialized the window; harvest from 0.
                    const harvestStart = (seekMode === 'dom_absolute') ? appliedStartRow : 0;
                    const limit = Math.min(trCollection.length || 0, harvestStart + maxRows);
                    for (let i = harvestStart; i < limit; i += 1) {
                        const tr = trCollection[i];
                        if (!tr) continue;
                        const tdNodes = tr.cells || tr.querySelectorAll('td');
                        const cells = [];
                        for (let c = 0; c < tdNodes.length; c += 1) {
                            cells.push((tdNodes[c].textContent || '').trim());
                        }
                        const anchors = tr.querySelectorAll('a[href]');
                        let detailUrl = null;
                        let pdfUrl = null;
                        for (let a = 0; a < anchors.length; a += 1) {
                            const href = anchors[a].href || '';
                            if (!href) continue;
                            if (!pdfUrl && href.toLowerCase().endsWith('.pdf')) {
                                pdfUrl = href;
                            } else if (!detailUrl) {
                                detailUrl = href;
                            }
                        }
                        if (!detailUrl) {
                            const readControl = tr.querySelector(
                                'input.courtWiseSearchBtn[casetypeid], .courtWiseSearchBtn[casetypeid], [casetypeid]'
                            );
                            const caseTypeId = readControl && readControl.getAttribute('casetypeid')
                                ? readControl.getAttribute('casetypeid').trim()
                                : '';
                            if (caseTypeId) {
                                detailUrl = `${window.location.origin}/Login/ReferenceCaseLawSearch?CaseName=${encodeURIComponent(caseTypeId)}&court=&Row=0&bookName=undefined`;
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
                    const nextRoot = document.getElementById('archivedpatientGrid_next');
                    const nextLink = (nextRoot && nextRoot.querySelector('a'))
                        || document.querySelector('.dataTables_paginate a.next, a[rel="next"]');
                    const nextDisabled = nextLink
                        ? (nextLink.classList.contains('disabled') || (nextLink.parentElement && nextLink.parentElement.classList.contains('disabled')))
                        : true;
                    const domTotalRows = trCollection.length || 0;
                    const resolvedTotalRows = Number.isFinite(totalRows) && totalRows >= 0
                        ? totalRows
                        : domTotalRows;
                    return {
                        headers,
                        rows,
                        next_url: !nextDisabled && nextLink && nextLink.href ? nextLink.href : null,
                        body_preview: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 400),
                        has_logout: Boolean(document.querySelector('a[href*="logout" i], a[href*="logoff" i]')),
                        total_rows: resolvedTotalRows,
                        requested_start_row: requestedStartRow,
                        start_row: appliedStartRow,
                        seek_mode: seekMode,
                        page_length: Number.isFinite(pageLength) && pageLength > 0 ? Math.floor(pageLength) : null,
                    };
                }""",
                        {"maxRows": max_rows, "startRow": safe_start_row},
                    )
                ),
                timeout=max(8.0, float(getattr(settings, "PLS_ARCHIVED_GRID_SNAPSHOT_TIMEOUT_SECONDS", 20) or 20)),
            )
        except asyncio.TimeoutError as exc:
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            logger.warning(
                "archivedpatientGrid compact snapshot timed out after %.1fs slot=%s max_rows=%s",
                elapsed,
                self.slot_number,
                max_rows,
            )
            if max_rows > 50:
                # The page is still open: try a smaller window before treating the browser as gone
                # (a reconnect re-renders the whole 10-16 MB grid and would time out the same way).
                smaller = max(50, max_rows // 2)
                logger.warning("archivedpatientGrid retrying compact snapshot with max_rows=%s", smaller)
                return await self._capture_archived_grid_snapshot(start_row=start_row, max_rows_override=smaller)
            raise BrowserDisconnected(
                f"archivedpatientGrid compact snapshot timed out after {elapsed:.1f}s"
            ) from exc
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        row_count = len((snapshot or {}).get("rows") or []) if snapshot else 0
        total_rows = (snapshot or {}).get("total_rows") if snapshot else None
        logger.info(
            "archivedpatientGrid compact snapshot done slot=%s rows=%s total_rows=%s start_row=%s seek_mode=%s elapsed=%.1fs",
            self.slot_number,
            row_count,
            total_rows,
            (snapshot or {}).get("start_row") if snapshot else None,
            (snapshot or {}).get("seek_mode") if snapshot else None,
            elapsed,
        )
        return snapshot

    @staticmethod
    def _render_compact_archived_grid_html(snapshot: Dict[str, Any]) -> str:
        # Body cells are always citation/title/court/read — ignore live DataTables headers
        # (often include a leading "#") so introspect column indexes stay aligned.
        headers = ["Citation", "Title", "Court", "Read"]
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

    async def _capture_html(self, *, resp=None, archived_grid_start_row: int = 0) -> tuple[str, Dict[str, Any]]:
        dom = await self._dom_shape()
        content_length = self._header_int(resp, "content-length")
        has_grid = bool(dom.get("has_archivedpatient_grid"))
        oversized = False
        if content_length is not None and content_length >= settings.PLAYWRIGHT_MAX_HTML_BYTES:
            oversized = True
        if int(dom.get("inputs") or 0) >= settings.PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD:
            oversized = True
        # CitationSearch often omits Content-Length (chunked/gzip) and can sit under the
        # input threshold while the live DOM is still ~10-16MB. Always compact when the
        # archived grid is present — never call page.content() on that surface.
        if has_grid:
            snapshot = await self._capture_archived_grid_snapshot(start_row=archived_grid_start_row)
            if snapshot:
                html_compact = self._render_compact_archived_grid_html(snapshot)
                return html_compact, {
                    "content_guard": "archivedpatientGrid_compact",
                    "inputs": int(dom.get("inputs") or 0),
                    "forms": int(dom.get("forms") or 0),
                    "content_length": content_length,
                    "rows": len(snapshot.get("rows") or []),
                    "row_cap": int(settings.PLS_ARCHIVED_GRID_MAX_ROWS),
                    "total_rows": snapshot.get("total_rows"),
                    "start_row": snapshot.get("start_row"),
                    "requested_start_row": snapshot.get("requested_start_row"),
                    "seek_mode": snapshot.get("seek_mode"),
                    "page_length": snapshot.get("page_length"),
                    "oversized_hint": oversized,
                }
            logger.warning(
                "archivedpatientGrid present but compact snapshot failed slot=%s url=%s; refusing page.content()",
                self.slot_number,
                self._page.url,
            )
            stub = self._render_oversize_stub(dom, content_length=content_length)
            return stub, {
                "content_guard": "archivedpatientGrid_snapshot_failed",
                "inputs": int(dom.get("inputs") or 0),
                "forms": int(dom.get("forms") or 0),
                "content_length": content_length,
                "grid_snapshot_failed": True,
            }
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

    async def _capture_case_description_modal(self) -> Dict[str, Any]:
        wait_ms = int(max(0.0, float(getattr(settings, "PLS_CASE_DESCRIPTION_WAIT_SECONDS", 6.0) or 0.0)) * 1000)
        return await self._wrap(
            self._page.evaluate(
                """async ({ waitMs }) => {
                const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                const findTrigger = () => document.querySelector(
                    'input.caseDescription[value="Case Description"], input.caseDescription'
                );
                let trigger = findTrigger();
                // The control is rendered by the page's own scripts after domcontentloaded; a page
                // classified before it appears would be recorded as headnote-only by mistake.
                const deadline = Date.now() + Math.max(0, Number(waitMs) || 0);
                while (!trigger && Date.now() < deadline) {
                    await sleep(150);
                    trigger = findTrigger();
                }
                if (!trigger) {
                    return {
                        case_description_selector_found: false,
                        case_description_modal_found: false,
                        case_description_modal_text: null,
                        case_description_modal_text_length: 0,
                    };
                }
                try {
                    if (trigger.scrollIntoView) {
                        trigger.scrollIntoView({ block: 'center', inline: 'nearest' });
                    }
                } catch (_scrollError) {}
                try {
                    trigger.click();
                } catch (_clickError) {
                    try {
                        trigger.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    } catch (_dispatchError) {}
                }
                let modal = null;
                let text = '';
                for (let i = 0; i < 80; i += 1) {
                    modal = document.querySelector('#ExceptionResponseScreen1');
                    text = modal && modal.innerText ? modal.innerText.trim() : '';
                    const hasBeforeMarker = /Before.+/i.test(text);
                    const hasReporterMarker = /CLC|SCMR|PLD/i.test(text);
                    if (text.length >= 2000 || hasBeforeMarker || hasReporterMarker) {
                        break;
                    }
                    await sleep(150);
                }
                return {
                    case_description_selector_found: true,
                    case_description_modal_found: Boolean(modal),
                    case_description_modal_text: text || null,
                    case_description_modal_text_length: text.length,
                };
            }""",
                {"waitMs": wait_ms},
            )
        )

    async def goto(self, url: str, **kwargs: Any) -> PageResult:
        archived_grid_start_row = kwargs.get("archived_grid_start_row", 0)
        capture_case_description_modal = bool(kwargs.get("capture_case_description_modal", False))
        resp = await self._wrap(
            self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.PLAYWRIGHT_TIMEOUT_MS,
            )
        )
        html_text, metadata = await self._capture_html(resp=resp, archived_grid_start_row=archived_grid_start_row)
        if capture_case_description_modal:
            try:
                modal_meta = await self._capture_case_description_modal()
                metadata.update(modal_meta or {})
            except Exception as exc:
                logger.warning(
                    "caseDescription modal capture failed slot=%s url=%s: %s",
                    self.slot_number,
                    self._page.url if self._page else url,
                    exc,
                )
                metadata["case_description_modal_error"] = str(exc)[:500]
        status = resp.status if resp else 200
        ctype = (resp.headers.get("content-type", "") if resp else "")
        requested_url = url
        final_url = self._page.url
        metadata["requested_url"] = requested_url
        metadata["final_url"] = final_url
        has_case_content = bool(re.search(r"Citation\s*Name\s*:", html_text or "", flags=re.IGNORECASE))
        requested_reference_path = bool(re.search(r"ReferenceCaseLawSearch", requested_url or "", flags=re.IGNORECASE))
        rewrite_login_check = (
            bool(re.search(r"/login/check(?:[/?#]|$)", final_url or "", flags=re.IGNORECASE))
            and (requested_reference_path or has_case_content)
        )
        result_url = requested_url if rewrite_login_check else final_url
        if rewrite_login_check:
            metadata["url_rewritten_from_login_check"] = True
        return PageResult(url=result_url, html=html_text, status=status, content_type=ctype, metadata=metadata)

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

    async def export_storage_state(self) -> Dict[str, Any]:
        """The live session (cookies + localStorage) as the site has renewed it during this run."""
        return await self._wrap(self._context.storage_state())

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
    preferred_slot_number: Optional[int] = None
    # Hash of the storage state the current browser was opened with; a live-state refresh is
    # written only while the slot still holds it (compare-and-update).
    opened_state_hash: Optional[str] = None
    # True for a reporter shard: it may never continue on the other shard's slot, because that slot
    # is in use by the other shard at the same time (one login per account on the site).
    exclusive_slot: bool = False

    async def open(self, slot: BrowserSessionSlot) -> Browser:
        state = self.manager.load_storage_state(slot)
        logger.info(
            "opening slot %s (logged in %s by %s): cookies %s",
            slot.slot_number,
            slot.logged_in_at.isoformat() if slot.logged_in_at else "unknown",
            slot.logged_in_by or "unknown",
            ", ".join(cookie_summary(state)) or "none",
        )
        self.browser = await self.factory(state, slot.slot_number)
        self.opened_state_hash = slot.storage_state_hash
        return self.browser

    async def ensure_browser(self) -> Browser:
        if self.browser is not None:
            return self.browser
        if self.preferred_slot_number:
            preferred = await self.manager.slot(self.preferred_slot_number)
            if preferred.state == "ACTIVE":
                return await self.open(preferred)
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
                if self.exclusive_slot:
                    raise
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
            if self.exclusive_slot:
                raise
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
