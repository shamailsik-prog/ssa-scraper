"""
Common interface, privacy guard, circuit breaker and daily budget for every extraction engine
(Amendment §2, §4, §21).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.extractors.schemas import EXTRACTION_TYPES
from scraper.models import SgaiUsageDaily
from scraper.security import contains_secret, is_login_session, resolve_is_safe, scrub_secrets, wrap_as_data

logger = logging.getLogger(__name__)

ENGINE_VERSION = "d15-1.0"


class PrivacyViolation(RuntimeError):
    """Raised before any network call when login-session material would leave the firm."""


class BudgetExhausted(RuntimeError):
    pass


class CircuitOpen(RuntimeError):
    pass


class InvalidEngineOutput(ValueError):
    pass


@dataclass
class ExtractionInput:
    source_name: str
    access_method: str
    content_hash: str
    html: Optional[str] = None
    text: Optional[str] = None
    markdown: Optional[str] = None
    url: Optional[str] = None
    source_meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def input_kind(self) -> str:
        if self.html:
            return "html"
        if self.markdown:
            return "markdown"
        return "text"

    def body(self) -> str:
        return self.html or self.markdown or self.text or ""

    @property
    def is_login_session(self) -> bool:
        return is_login_session(self.access_method)


@dataclass
class EngineResult:
    extraction_type: str
    engine_mode: str  # deterministic|managed|local|cache
    data: Optional[Dict[str, Any]]
    status: str  # ok|ai_failed|invalid_json|privacy_blocked|budget_exhausted|circuit_open|ai_skipped|cache_hit
    elapsed_ms: int = 0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    credits: Optional[float] = None
    error: Optional[str] = None
    request_id: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "cache_hit") and self.data is not None


@runtime_checkable
class StructuredExtractor(Protocol):
    async def extract_judgment(self, *, html=None, text=None, source_meta=None): ...

    async def extract_statute(self, *, html=None, text=None, source_meta=None): ...

    async def extract_instrument(self, *, html=None, text=None, source_meta=None): ...

    async def extract_result_rows(self, *, html, search_map=None): ...


# --------------------------------------------------------------------------- privacy
def assert_public_material(inp: ExtractionInput, engine_mode: str) -> None:
    """Managed cloud and any remote endpoint may only receive PUBLIC material. Never cookies/secrets."""
    if engine_mode == "managed":
        if inp.is_login_session:
            raise PrivacyViolation(f"{inp.source_name}: login_session content may not be sent to the managed ScrapeGraph API")
        if not settings.SGAI_MANAGED_PUBLIC_ONLY:
            raise PrivacyViolation("SGAI_MANAGED_PUBLIC_ONLY=false is not permitted")
    body = inp.body()
    if contains_secret(body[:500_000]):
        # Secrets never enter a prompt; the body is scrubbed by wrap_as_data anyway, but a secret in
        # page content means the page itself is suspect. Scrub and continue; the log records it.
        logger.warning("%s: credential-shaped content detected in page; scrubbed before extraction", inp.source_name)
    for forbidden in ("cookies", "storage_state", "authorization"):
        if forbidden in inp.source_meta:
            raise PrivacyViolation(f"source_meta contains forbidden key {forbidden!r}")


def local_endpoint_is_on_prem(base_url: str) -> bool:
    """A local engine must be private/loopback or an explicitly trusted on-prem host."""
    host = (urlsplit(base_url).hostname or "").lower()
    if not host:
        return False
    trusted = {h.strip().lower() for h in settings.SGAI_LOCAL_TRUSTED_HOSTS.split(",") if h.strip()}
    if host in trusted:
        return True
    if host in ("localhost", "ollama", "local-ai") or host.endswith(".local") or host.endswith(".internal") or host.endswith(".lan"):
        return True
    # private address space → on-prem; public or unresolvable → not acceptable for login-session text
    import ipaddress as _ip
    import socket as _socket

    from scraper.security import is_private_address

    try:
        _ip.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = [i[4][0] for i in _socket.getaddrinfo(host, None)]
        except Exception:
            return False
    return bool(addresses) and all(is_private_address(a) for a in addresses)


# --------------------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    def __init__(self, threshold: int, cooldown_seconds: int):
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self.failures = 0
        self.opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.cooldown:
            self.opened_at = None
            self.failures = 0
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            logger.error("ScrapeGraph circuit breaker OPEN after %d consecutive failures", self.failures)

    def reset(self) -> None:
        self.failures = 0
        self.opened_at = None


MANAGED_BREAKER = CircuitBreaker(settings.SGAI_CIRCUIT_BREAKER_FAILURES, settings.SGAI_CIRCUIT_BREAKER_COOLDOWN_SECONDS)
LOCAL_BREAKER = CircuitBreaker(settings.SGAI_CIRCUIT_BREAKER_FAILURES, settings.SGAI_CIRCUIT_BREAKER_COOLDOWN_SECONDS)


# --------------------------------------------------------------------------- daily budget / usage
async def usage_row(db: AsyncSession, engine_mode: str, source_name: str = "*") -> SgaiUsageDaily:
    today = datetime.now(timezone.utc).date()
    row = (
        await db.execute(select(SgaiUsageDaily).where(SgaiUsageDaily.day == today, SgaiUsageDaily.engine_mode == engine_mode, SgaiUsageDaily.source_name == source_name))
    ).scalars().first()
    if row is None:
        row = SgaiUsageDaily(day=today, engine_mode=engine_mode, source_name=source_name)
        db.add(row)
        await db.flush()
    return row


async def managed_credits_used_today(db: AsyncSession) -> float:
    today = datetime.now(timezone.utc).date()
    rows = (await db.execute(select(SgaiUsageDaily).where(SgaiUsageDaily.day == today, SgaiUsageDaily.engine_mode == "managed"))).scalars().all()
    return float(sum(r.credits for r in rows)) + float(sum(r.calls for r in rows if r.credits == 0))


async def check_budget(db: AsyncSession) -> None:
    cap = settings.SGAI_DAILY_CREDIT_CAP
    if cap is None:
        return
    used = await managed_credits_used_today(db)
    if used >= cap:
        logger.error("SGAI_BUDGET_EXHAUSTED: %.1f of %d credits used today", used, cap)
        raise BudgetExhausted(f"SGAI_BUDGET_EXHAUSTED ({used:.1f}/{cap})")


async def record_usage(db: AsyncSession, engine_mode: str, source_name: str, result: EngineResult, conflicts: int = 0) -> None:
    for scope in ("*", source_name):
        row = await usage_row(db, engine_mode, scope)
        if result.status == "cache_hit":
            row.cache_hits += 1
        else:
            row.calls += 1
            row.credits += float(result.credits or 0.0)
            if result.ok:
                row.successes += 1
            else:
                row.failures += 1
        row.conflicts += conflicts


# --------------------------------------------------------------------------- output parsing
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def coerce_engine_output(extraction_type: str, raw: Any) -> Dict[str, Any]:
    """Turn whatever the engine returned into a schema-validated dict or raise InvalidEngineOutput."""
    model = EXTRACTION_TYPES[extraction_type]
    data = raw
    if isinstance(raw, dict) and "result" in raw and extraction_type not in raw:
        data = raw["result"]
    if isinstance(data, str):
        m = _JSON_BLOCK.search(data)
        if not m:
            raise InvalidEngineOutput("engine returned no JSON object")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise InvalidEngineOutput(f"engine returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidEngineOutput(f"engine returned {type(data).__name__}, expected object")
    # scrub any secret-shaped strings that may have been echoed
    data = json.loads(scrub_secrets(json.dumps(data, default=str)))
    try:
        parsed = model.model_validate(data)
    except ValidationError as exc:
        # Drop unknown keys (a page cannot add fields) and retry once.
        allowed = set(model.model_fields)
        pruned = {k: v for k, v in data.items() if k in allowed}
        try:
            parsed = model.model_validate(pruned)
        except ValidationError as exc2:
            raise InvalidEngineOutput(f"schema violation: {exc2.errors()[:3]}") from exc
    return json.loads(parsed.model_dump_json())


class BaseScrapeGraphEngine:
    """Shared plumbing. Subclasses implement `_call(extraction_type, prompt, inp)` and `engine_mode`."""

    engine_mode = "base"
    breaker: CircuitBreaker = CircuitBreaker(10**9, 1)

    async def extract(self, extraction_type: str, inp: ExtractionInput) -> EngineResult:
        from scraper.extractors.prompts import build_prompt

        started = time.monotonic()
        if self.breaker.is_open:
            return EngineResult(extraction_type, self.engine_mode, None, "circuit_open", error="circuit breaker open")
        try:
            assert_public_material(inp, self.engine_mode)
        except PrivacyViolation as exc:
            return EngineResult(extraction_type, self.engine_mode, None, "privacy_blocked", error=str(exc))
        prompt = build_prompt(extraction_type)
        payload_text = wrap_as_data(inp.body())
        last_error: Optional[str] = None
        for attempt in range(settings.SGAI_MAX_RETRIES + 1):
            try:
                raw, meta = await asyncio.wait_for(self._call(extraction_type, prompt, payload_text, inp), timeout=settings.SGAI_TIMEOUT_SECONDS)
                data = coerce_engine_output(extraction_type, raw)
                self.breaker.record_success()
                return EngineResult(
                    extraction_type,
                    self.engine_mode,
                    data,
                    "ok",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    prompt_tokens=meta.get("prompt_tokens"),
                    completion_tokens=meta.get("completion_tokens"),
                    credits=meta.get("credits"),
                    request_id=meta.get("request_id"),
                )
            except InvalidEngineOutput as exc:
                last_error = str(exc)
                self.breaker.record_failure()
                status = "invalid_json"
            except asyncio.TimeoutError:
                last_error = f"timeout after {settings.SGAI_TIMEOUT_SECONDS}s"
                self.breaker.record_failure()
                status = "ai_failed"
            except PrivacyViolation as exc:
                return EngineResult(extraction_type, self.engine_mode, None, "privacy_blocked", error=str(exc))
            except Exception as exc:  # network, SDK, HTTP
                last_error = scrub_secrets(str(exc))[:500]
                self.breaker.record_failure()
                status = "ai_failed"
            if attempt < settings.SGAI_MAX_RETRIES:
                await asyncio.sleep(min(2 ** attempt, 8))
        return EngineResult(extraction_type, self.engine_mode, None, status, elapsed_ms=int((time.monotonic() - started) * 1000), error=last_error)

    async def _call(self, extraction_type: str, prompt: str, payload_text: str, inp: ExtractionInput):
        raise NotImplementedError
