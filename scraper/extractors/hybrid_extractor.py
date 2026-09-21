"""
Hybrid extraction orchestrator (Amendment §4, §7, §8, §21).

    deterministic first → accept when confident and complete →
    otherwise the PERMITTED engine (login_session ⇒ local only; public ⇒ managed or local) →
    cache → budget → circuit breaker → call → reconcile field by field → validate →
    extraction_audit row → usage ledger.

The caller never learns which engine produced the result; it receives an ExtractionOutcome.
AI availability is never a prerequisite: every failure path falls open to the deterministic
result (SGAI_FAIL_OPEN_TO_DETERMINISTIC) and quarantines what stays below the threshold.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.extractors import deterministic as det
from scraper.extractors.cache import cache_get, cache_put
from scraper.extractors.schemas import SCHEMA_VERSION
from scraper.extractors.scrapegraph_base import (
    BudgetExhausted,
    ENGINE_VERSION,
    EngineResult,
    ExtractionInput,
    check_budget,
    record_usage,
)
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.extractors.scrapegraph_managed import ManagedScrapeGraphEngine
from scraper.extractors.validation import (
    MANDATORY_INSTRUMENT_FIELDS,
    MANDATORY_JUDGMENT_FIELDS,
    MANDATORY_STATUTE_FIELDS,
    ValidationOutcome,
    mandatory_present,
    reconcile_instrument,
    reconcile_judgment,
    reconcile_result_rows,
    reconcile_statute,
)
from scraper.models import Court, ExtractionAudit, ScraperSource
from scraper.security import is_login_session

logger = logging.getLogger(__name__)


@dataclass
class ExtractionOutcome:
    extraction_type: str
    data: Dict[str, Any]
    confidence: float
    engine: str  # deterministic|hybrid:managed|hybrid:local|hybrid:cache|scrapegraph_managed|scrapegraph_local
    ai_status: str  # none|ok|cache_hit|ai_failed|invalid_json|privacy_blocked|budget_exhausted|circuit_open|ai_skipped
    conflicts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    quarantine: bool = False
    quarantine_reason: Optional[str] = None
    audit_id: Optional[Any] = None
    deterministic_json: Optional[Dict[str, Any]] = None
    ai_json: Optional[Dict[str, Any]] = None


async def load_court_directory(db: AsyncSession) -> Dict[str, str]:
    directory: Dict[str, str] = {}
    for c in (await db.execute(select(Court))).scalars().all():
        directory[c.name.lower()] = c.name
        directory[c.short_code.lower()] = c.name
        for a in c.aliases or []:
            directory[a.lower().replace(".", "")] = c.name
    return directory


class HybridExtractor:
    """StructuredExtractor implementation bound to one source and one DB session."""

    def __init__(
        self,
        db: AsyncSession,
        source: ScraperSource,
        *,
        managed: Optional[ManagedScrapeGraphEngine] = None,
        local: Optional[LocalScrapeGraphEngine] = None,
        court_directory: Optional[Dict[str, str]] = None,
        provenance_id=None,
        staging_id=None,
        force_ai: bool = False,
    ):
        self.db = db
        self.force_ai = force_ai  # always consult the permitted engine (operator compare mode / tests)
        self.source = source
        self.managed = managed if managed is not None else ManagedScrapeGraphEngine()
        self.local = local if local is not None else LocalScrapeGraphEngine()
        self._court_directory = court_directory
        self.provenance_id = provenance_id
        self.staging_id = staging_id

    # ------------------------------------------------------------------ policy
    @property
    def min_confidence(self) -> float:
        return float(self.source.extraction_min_confidence or settings.SGAI_DEFAULT_MIN_CONFIDENCE)

    def permitted_engine(self):
        """Return (engine, mode_name) or (None, reason) honouring privacy and per-source policy."""
        mode = (self.source.extraction_mode or settings.SGAI_MODE or "hybrid").lower()
        if not settings.SGAI_ENABLED or not self.source.ai_extract_enabled or mode == "deterministic":
            return None, "ai_skipped"
        login = is_login_session(self.source.access_method)
        if login:
            # Login-session material: local engine only; managed is never permitted.
            if mode == "scrapegraph_managed":
                return None, "privacy_blocked"
            if self.local.configured:
                return self.local, "local"
            return None, "ai_skipped"
        if mode == "scrapegraph_local":
            return (self.local, "local") if self.local.configured else (None, "ai_skipped")
        if mode == "scrapegraph_managed":
            return (self.managed, "managed") if self.managed.configured else (None, "ai_skipped")
        # hybrid: managed preferred for public when configured, else local, else none
        if self.managed.configured:
            return self.managed, "managed"
        if self.local.configured:
            return self.local, "local"
        return None, "ai_skipped"

    async def court_directory(self) -> Dict[str, str]:
        if self._court_directory is None:
            self._court_directory = await load_court_directory(self.db)
        return self._court_directory

    # ------------------------------------------------------------------ core
    async def _run(
        self,
        extraction_type: str,
        inp: ExtractionInput,
        deterministic: Dict[str, Any],
        mandatory,
        reconcile,
        *,
        deterministic_only: bool = False,
    ) -> ExtractionOutcome:
        started = time.monotonic()
        det_conf = float(deterministic.get("extractor_confidence") or 0.0)
        engine, mode = self.permitted_engine()
        if deterministic_only:
            engine, mode = None, "ai_skipped"
        ai_json: Optional[Dict[str, Any]] = None
        ai_result: Optional[EngineResult] = None
        ai_status = mode if engine is None else "pending"
        # B. accept deterministic output without spending AI when confident and complete
        if engine is not None and not self.force_ai and det_conf >= self.min_confidence and mandatory_present(deterministic, mandatory):
            ai_status = "ai_skipped"
            engine = None
        if engine is not None:
            cached = await cache_get(self.db, content_hash=inp.content_hash, schema_version=self.source.scrapegraph_schema_version or SCHEMA_VERSION, extraction_type=extraction_type, engine_mode=mode)
            if cached is not None:
                ai_json = cached
                ai_result = EngineResult(extraction_type, mode, cached, "cache_hit")
                ai_status = "cache_hit"
            else:
                try:
                    if mode == "managed":
                        await check_budget(self.db)
                    ai_result = await engine.extract(extraction_type, inp)
                    ai_status = ai_result.status
                    if ai_result.ok:
                        ai_json = ai_result.data
                        await cache_put(self.db, content_hash=inp.content_hash, schema_version=self.source.scrapegraph_schema_version or SCHEMA_VERSION, extraction_type=extraction_type, engine_mode=mode, result=ai_json)
                    else:
                        self.source.last_ai_error = f"{ai_result.status}: {ai_result.error}"[:2000]
                except BudgetExhausted as exc:
                    ai_status = "budget_exhausted"
                    ai_result = EngineResult(extraction_type, mode, None, "budget_exhausted", error=str(exc))
                    self.source.last_ai_error = str(exc)[:2000]
            if ai_result is not None:
                await record_usage(self.db, mode, self.source.source_name, ai_result)
        if not settings.SGAI_FAIL_OPEN_TO_DETERMINISTIC and ai_json is None and engine is not None:
            logger.error("%s: AI extraction failed and fail-open is disabled; record quarantined", self.source.source_name)
        outcome: ValidationOutcome = reconcile(deterministic, ai_json)
        if ai_json is None and engine is not None and not settings.SGAI_FAIL_OPEN_TO_DETERMINISTIC:
            outcome.quarantine = True
            outcome.quarantine_reason = f"AI extraction unavailable ({ai_status}) and SGAI_FAIL_OPEN_TO_DETERMINISTIC=false"
        engine_label = "deterministic" if ai_json is None else ("hybrid:cache" if ai_status == "cache_hit" else f"hybrid:{mode}")
        if ai_json is not None and (self.source.extraction_mode or "").startswith("scrapegraph_"):
            engine_label = self.source.extraction_mode
        audit = ExtractionAudit(
            source_provenance_id=self.provenance_id,
            staging_id=self.staging_id,
            source_name=self.source.source_name,
            extractor=engine_label,
            extractor_version=f"{det.DETERMINISTIC_VERSION}+{ENGINE_VERSION}",
            schema_version=self.source.scrapegraph_schema_version or SCHEMA_VERSION,
            content_hash=inp.content_hash,
            input_kind=inp.input_kind,
            ai_mode=(mode if engine is not None or ai_status == "cache_hit" else "none") if ai_status != "ai_skipped" else "none",
            deterministic_json=_trim(deterministic),
            ai_json=_trim(ai_json) if ai_json else None,
            reconciled_json=_trim(outcome.data),
            conflicts_json=outcome.conflicts,
            validation_errors_json=outcome.errors,
            prompt_tokens=getattr(ai_result, "prompt_tokens", None),
            completion_tokens=getattr(ai_result, "completion_tokens", None),
            credits_or_cost=getattr(ai_result, "credits", None),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            status="ok" if ai_json is not None or ai_status in ("ai_skipped", "none") else ai_status,
        )
        self.db.add(audit)
        await self.db.flush()
        if outcome.conflicts and ai_result is not None:
            await record_usage(self.db, mode, self.source.source_name, EngineResult(extraction_type, mode, None, "cache_hit"), conflicts=len(outcome.conflicts))
            # the helper above counts a cache hit; undo that side effect
            from scraper.extractors.scrapegraph_base import usage_row

            for scope in ("*", self.source.source_name):
                row = await usage_row(self.db, mode, scope)
                row.cache_hits -= 1
        return ExtractionOutcome(
            extraction_type=extraction_type,
            data=outcome.data,
            confidence=outcome.confidence,
            engine=engine_label,
            ai_status=ai_status if ai_status != "pending" else "none",
            conflicts=outcome.conflicts,
            errors=outcome.errors,
            quarantine=outcome.quarantine,
            quarantine_reason=outcome.quarantine_reason,
            audit_id=audit.id,
            deterministic_json=deterministic,
            ai_json=ai_json,
        )

    # ------------------------------------------------------------------ StructuredExtractor
    def _input(self, *, html, text, source_meta, content_hash) -> ExtractionInput:
        from scraper.fetchers import sha256_text

        body = html or text or ""
        return ExtractionInput(
            source_name=self.source.source_name,
            access_method=self.source.access_method,
            content_hash=content_hash or sha256_text(body),
            html=html,
            text=text if not html else None,
            url=(source_meta or {}).get("url"),
            source_meta=dict(source_meta or {}),
        )

    async def extract_judgment(
        self,
        *,
        html=None,
        text=None,
        source_meta=None,
        content_hash=None,
        deterministic_only: bool = False,
    ) -> ExtractionOutcome:
        from scraper.parsers.text_cleaner import clean_html

        raw_text = text or (clean_html(html) if html else "")
        deterministic = det.extract_judgment_deterministic(html=html, text=raw_text, source_meta=source_meta)
        inp = self._input(html=html, text=raw_text, source_meta=source_meta, content_hash=content_hash)
        directory = await self.court_directory()
        return await self._run(
            "judgment",
            inp,
            deterministic,
            MANDATORY_JUDGMENT_FIELDS,
            lambda d, a: reconcile_judgment(
                deterministic=d,
                ai=a,
                raw_text=raw_text,
                source_url=(source_meta or {}).get("url"),
                raw_html=html,
                court_directory=directory,
                min_confidence=self.min_confidence,
            ),
            deterministic_only=deterministic_only,
        )

    async def extract_statute(self, *, html=None, text=None, source_meta=None, content_hash=None) -> ExtractionOutcome:
        from scraper.parsers.text_cleaner import clean_html

        raw_text = text or (clean_html(html) if html else "")
        deterministic = det.extract_statute_deterministic(html=html, text=raw_text, source_meta=source_meta)
        inp = self._input(html=html, text=raw_text, source_meta=source_meta, content_hash=content_hash)
        return await self._run("statute", inp, deterministic, MANDATORY_STATUTE_FIELDS, lambda d, a: reconcile_statute(deterministic=d, ai=a, raw_text=raw_text, min_confidence=self.min_confidence))

    async def extract_instrument(self, *, html=None, text=None, source_meta=None, content_hash=None) -> ExtractionOutcome:
        from scraper.parsers.text_cleaner import clean_html

        raw_text = text or (clean_html(html) if html else "")
        deterministic = det.extract_instrument_deterministic(html=html, text=raw_text, source_meta=source_meta)
        inp = self._input(html=html, text=raw_text, source_meta=source_meta, content_hash=content_hash)
        return await self._run("instrument", inp, deterministic, MANDATORY_INSTRUMENT_FIELDS, lambda d, a: reconcile_instrument(deterministic=d, ai=a, raw_text=raw_text, min_confidence=self.min_confidence))

    async def extract_result_rows(self, *, html, search_map=None, base_url: str = "", content_hash=None) -> ExtractionOutcome:
        deterministic = det.extract_result_rows_deterministic(html=html, search_map=search_map, base_url=base_url)
        inp = self._input(html=html, text=None, source_meta={"url": base_url}, content_hash=content_hash)
        return await self._run("result_rows", inp, deterministic, ("result_rows",), lambda d, a: reconcile_result_rows(deterministic=d, ai=a, html=html))


def _trim(d: Optional[Dict[str, Any]], limit: int = 20000) -> Optional[Dict[str, Any]]:
    """Keep audit rows bounded: long text fields are replaced by length + hash."""
    if d is None:
        return None
    from scraper.fetchers import canonical_text_hash

    out: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = {"_len": len(v), "_sha256_canonical": canonical_text_hash(v)}
        else:
            out[k] = v
    return out
