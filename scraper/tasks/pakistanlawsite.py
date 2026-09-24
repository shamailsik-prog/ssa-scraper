"""
PakistanLawSite — SEARCH-DRIVEN, four-tier coverage inside a human-login Playwright session
(Amendment §9, §10; Cursor command §2).

TIER 1 citation enumeration (backbone): for each subscribed reporter × year, probe citation
        page numbers 1, 2, 3 … ; VOLUME_END_GAP consecutive misses close the volume.
TIER 2 statute/section enumeration: one query per promoted statute section.
TIER 3 vocabulary sweep: ranked by yield, retired after repeated zero yield.
TIER 4 current-year daily wave: each subscribed reporter for the current year, daily.

crawl_frontier and crawl_coverage are the truth for progress. A judgment reached by several
routes is stored once (same content hash → same provenance/staging row) with every route kept.
Raw-first: provenance + staging rows exist before any extraction. Extraction for this source is
deterministic + LOCAL engine only — never managed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.auth.session_manager import (
    Browser,
    BrowserDisconnected,
    ContinuityRunner,
    LoginRequired,
    NoActiveSlot,
    PageResult,
    SearchFormSubmissionError,
    SessionLock,
    SessionLockHeld,
    merge_source_config,
    SessionManager,
    playwright_browser_factory,
    raise_for_verdict,
)
from scraper.config import settings
from scraper.extractors.hybrid_extractor import HybridExtractor
from scraper.extractors.judgment_guards import (
    detect_headnotes_only,
    extract_before_jj_judge_names,
    strip_leading_judgment_chrome,
)
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.fetchers import record_provenance, stage_judgment
from scraper.models import CrawlCoverage, CrawlFrontier, ScraperJob, ScraperSource, StatuteSection, Statute
from scraper.notify import notify
from scraper.parsers.citation_extractor import normalise_citation
from scraper.parsers.text_cleaner import clean_html
from scraper.security import ExplicitBlock, VerificationRequired
from scraper.tasks.search_map import active_map, map_as_dict, map_search_form, mark_map_stale, record_parse_result

logger = logging.getLogger(__name__)

SOURCE_NAME = "PakistanLawSite"
TIER3_RETIRE_AFTER = 3
TIER4_HIGH_YIELD_TERMS = 10
PACING_KEY = "pacing"  # source.config_json.pacing: hour / hour_pages / day / day_pages (specification 3.6)


class PacingBudgetExceeded(RuntimeError):
    """PAGES_PER_HOUR / PAGES_PER_DAY spent; the run ends, the source stays ACTIVE and Beat resumes it later."""


class SearchMapStale(RuntimeError):
    """The saved CitationSearch map no longer describes a usable result page."""


def _aggregate_legacy_pacing(cfg: Dict[str, Any], *, hour_key: str, day_key: str) -> Dict[str, Any]:
    """Sum the `pacing_slot_<n>` counters an earlier release kept per login slot, for the current
    hour and day only, into one `pacing` object."""
    hour_pages = 0
    day_pages = 0
    for name, value in cfg.items():
        if not (isinstance(name, str) and name.startswith("pacing_slot_") and isinstance(value, dict)):
            continue
        if value.get("hour") == hour_key:
            hour_pages += int(value.get("hour_pages", 0) or 0)
        if value.get("day") == day_key:
            day_pages += int(value.get("day_pages", 0) or 0)
    if not hour_pages and not day_pages:
        return {}
    return {"hour": hour_key, "hour_pages": hour_pages, "day": day_key, "day_pages": day_pages}


DEFAULT_VOCABULARY = []  # firm value: seeded from PLS_TIER3_VOCABULARY or the dashboard; never invented here


# --------------------------------------------------------------------------- frontier seeding
async def seed_frontier(db: AsyncSession, source: ScraperSource) -> Dict[str, int]:
    """Idempotently create frontier rows for all four tiers. Firm values that are blank leave the
    corresponding tier idle (conservative default, logged)."""
    now = datetime.now(timezone.utc)
    counts = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0}
    reporters = settings.subscribed_reporters
    current_year = now.year
    earliest = settings.PLS_EARLIEST_YEAR or current_year
    if not reporters:
        logger.warning("PLS_SUBSCRIBED_REPORTERS is NOT CONFIGURED; Tier 1 and Tier 4 idle")
    if not settings.PLS_EARLIEST_YEAR:
        logger.warning("PLS_EARLIEST_YEAR is NOT CONFIGURED; Tier 1 covers the current year only")
    existing = {(r.tier, r.query_key) for r in (await db.execute(select(CrawlFrontier).where(CrawlFrontier.source_name == SOURCE_NAME))).scalars().all()}
    coverage = {(c.reporter, c.year) for c in (await db.execute(select(CrawlCoverage).where(CrawlCoverage.source_name == SOURCE_NAME))).scalars().all()}
    for rep in reporters:
        for year in range(current_year, earliest - 1, -1):
            key = f"t1:{rep}:{year}"
            if (1, key) not in existing:
                db.add(CrawlFrontier(source_name=SOURCE_NAME, tier=1, query_key=key, query_json={"reporter": rep, "year": year}, cursor_json={"page_no": 1}, priority=10 + (current_year - year)))
                counts["tier1"] += 1
            if (rep, year) not in coverage:
                db.add(CrawlCoverage(source_name=SOURCE_NAME, reporter=rep, year=year)
)
        key4 = f"t4:{rep}:{current_year}"
        if (4, key4) not in existing:
            db.add(CrawlFrontier(source_name=SOURCE_NAME, tier=4, query_key=key4, query_json={"reporter": rep, "year": current_year, "daily": True}, cursor_json={"page": 1}, priority=1, next_run_at=now))
            counts["tier4"] += 1
    # Tier 2 from promoted statute sections (bounded per seeding run)
    rows = (await db.execute(select(StatuteSection, Statute).join(Statute, Statute.id == StatuteSection.statute_id).limit(2000))).all()
    for sec, st in rows:
        key = f"t2:{st.short_name or st.name}:{sec.section_number}"
        if (2, key) not in existing:
            db.add(CrawlFrontier(source_name=SOURCE_NAME, tier=2, query_key=key, query_json={"statute": st.short_name or st.name, "section": sec.section_number}, cursor_json={"page": 1}, priority=50))
            counts["tier2"] += 1
            existing.add((2, key))
    high_yield = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.source_name == SOURCE_NAME, CrawlFrontier.tier == 3, CrawlFrontier.yield_count > 0).order_by(CrawlFrontier.yield_count.desc()).limit(TIER4_HIGH_YIELD_TERMS))).scalars().all()
    for hy in high_yield:
        key = f"t4:vocab:{hy.query_json.get('keyword', '').lower()}"
        if (4, key) not in existing and hy.query_json.get("keyword"):
            db.add(CrawlFrontier(source_name=SOURCE_NAME, tier=4, query_key=key, query_json={"keyword": hy.query_json["keyword"], "daily": True}, cursor_json={"page": 1}, priority=5, next_run_at=now))
            counts["tier4"] += 1
            existing.add((4, key))
    for term in settings.tier3_vocabulary or DEFAULT_VOCABULARY:
        key = f"t3:{term.lower()}"
        if (3, key) not in existing:
            db.add(CrawlFrontier(source_name=SOURCE_NAME, tier=3, query_key=key, query_json={"keyword": term}, cursor_json={"page": 1}, priority=80))
            counts["tier3"] += 1
    await db.flush()
    return counts


# --------------------------------------------------------------------------- query building
def build_values(search_map: Dict[str, Any], query: Dict[str, Any], cursor: Dict[str, Any]) -> Dict[str, str]:
    """Map a frontier query onto the form roles found in the search map."""
    fields = search_map.get("fields") or {}
    values: Dict[str, str] = {}
    if "reporter" in query:
        if "reporter" in fields:
            values["reporter"] = str(query["reporter"])
        if "year" in fields:
            values["year"] = str(query["year"])
        if "page_no" in cursor:
            if "page" in fields:
                values["page"] = str(cursor["page_no"])
            elif "citation" in fields:
                values["citation"] = str(cursor["page_no"])
            elif "keyword" in fields:
                values["keyword"] = f"{query['year']} {query['reporter']} {cursor['page_no']}"
        if ("reporter" not in fields or "year" not in fields) and "keyword" in fields and "page_no" not in cursor:
            values["keyword"] = f"{query['reporter']} {query['year']}"
    if "statute" in query:
        if "statute" in fields:
            values["statute"] = str(query["statute"])
            if "section" in fields:
                values["section"] = str(query["section"])
        elif "keyword" in fields:
            values["keyword"] = f"{query['statute']} section {query['section']}"
    if "keyword" in query and "keyword" in fields:
        values["keyword"] = str(query["keyword"])
    return values


def unmapped_query_reason(search_map: Dict[str, Any], query: Dict[str, Any], cursor: Dict[str, Any]) -> Optional[str]:
    """Explain why no safely mapped form field can express a frontier query."""
    values = build_values(search_map, query, cursor)
    fields = search_map.get("fields") or {}
    if "reporter" in query:
        if {"reporter", "year"}.issubset(fields) and ({"page", "citation"} & set(fields)):
            return None
        if "keyword" in fields:
            return None
        missing = [role for role in ("reporter", "year", "page", "citation", "keyword") if role not in fields]
        return f"search map cannot express reporter citation query; missing usable roles: {', '.join(missing)}"
    if "statute" in query:
        if "statute" in fields or "keyword" in fields:
            return None
        missing = [role for role in ("statute", "section", "keyword") if role not in fields]
        return f"search map cannot express statute query; missing usable roles: {', '.join(missing)}"
    if "keyword" in query:
        return None if "keyword" in fields else "search map cannot express keyword query; missing usable role: keyword"
    if values:
        return None
    return "search map cannot express frontier query; no usable mapped fields"


# --------------------------------------------------------------------------- pipeline
class PakistanLawSitePipeline:
    def __init__(
        self,
        db: AsyncSession,
        source: ScraperSource,
        *,
        browser_factory: Callable = playwright_browser_factory,
        local_engine: Optional[LocalScrapeGraphEngine] = None,
        sleep=asyncio.sleep,
        redis_client=None,
        job_id=None,
    ):
        self.db = db
        self.source = source
        self.manager = SessionManager(db, source)
        self.runner = ContinuityRunner(self.manager, browser_factory, sleep=sleep)
        self.local_engine = local_engine if local_engine is not None else LocalScrapeGraphEngine()
        self.redis_client = redis_client
        self.job_id = job_id
        self.sleep = sleep
        self._session_lock: Optional[SessionLock] = None
        self.stats = {"queries": 0, "pages": 0, "rows": 0, "staged": 0, "duplicates": 0, "misses": 0, "volumes_closed": 0, "halted": False, "paused": False, "pacing_paused": False, "pages_charged": 0}

    # ---------------------------------------------------------------- pacing (LOGIN_DELAY_*, PAGES_PER_*)
    async def _charge_page(self) -> None:
        """Specification 3.6: before every result-page submission and every detail fetch, refresh
        the lock, count the page against the hourly and daily budgets in source.config_json.pacing,
        raise PacingBudgetExceeded when a budget is spent, otherwise sleep LOGIN_DELAY_MIN..MAX."""
        if self._session_lock is not None:
            await self._session_lock.refresh()
        now = datetime.now(timezone.utc)
        key = PACING_KEY
        cfg = dict(self.source.config_json or {})
        pacing = dict(cfg.get(key) or {})
        hour_key = now.strftime("%Y-%m-%dT%H")
        day_key = now.strftime("%Y-%m-%d")
        if not pacing:
            # First run after the switch from per-slot counters: what the earlier release charged
            # this hour and today still counts against the budgets.
            pacing = _aggregate_legacy_pacing(cfg, hour_key=hour_key, day_key=day_key)
        if pacing.get("hour") != hour_key:
            pacing["hour"], pacing["hour_pages"] = hour_key, 0
        if pacing.get("day") != day_key:
            pacing["day"], pacing["day_pages"] = day_key, 0
        pacing["hour_pages"] = int(pacing.get("hour_pages", 0)) + 1
        pacing["day_pages"] = int(pacing.get("day_pages", 0)) + 1
        await merge_source_config(self.db, self.source, {key: pacing})
        self.stats["pages_charged"] += 1
        await self._heartbeat_job()
        await self.db.flush()
        if pacing["day_pages"] > settings.PAGES_PER_DAY:
            raise PacingBudgetExceeded(f"PAGES_PER_DAY={settings.PAGES_PER_DAY} spent for {day_key}")
        if pacing["hour_pages"] > settings.PAGES_PER_HOUR:
            raise PacingBudgetExceeded(f"PAGES_PER_HOUR={settings.PAGES_PER_HOUR} spent for {hour_key}")
        await self.sleep(random.uniform(float(settings.LOGIN_DELAY_MIN), float(settings.LOGIN_DELAY_MAX)))

    async def _heartbeat_job(self) -> None:
        """Mirror live counters onto the running scraper_jobs row so the dispatcher can tell a live
        job from one whose worker container was recreated mid-run (updated_at is the heartbeat)."""
        if self.job_id is None:
            return
        await self.db.execute(
            update(ScraperJob)
            .where(ScraperJob.id == self.job_id, ScraperJob.status == "running")
            .values(
                result_summary=dict(self.stats),
                pages_scraped=int(self.stats.get("pages_charged", 0) or 0),
                records_extracted=int(self.stats.get("staged", 0) or 0),
            )
        )

    async def _persist_live_session(self) -> None:
        """Store the browser's current cookies back into its slot (same human login, renewed by the
        site during the run) so the next job does not start from the cookies captured at login time."""
        browser = self.runner.browser
        exporter = getattr(browser, "export_storage_state", None)
        if browser is None or not callable(exporter):
            return
        try:
            state = await exporter()
        except Exception as exc:
            logger.warning("PakistanLawSite: live session export failed for slot %s: %s", getattr(browser, "slot_number", "?"), exc)
            return
        new_hash = await self.manager.refresh_storage_state(
            browser.slot_number, state, expected_hash=self.runner.opened_state_hash
        )
        # Commit at once: the slot row must not stay locked by an open transaction after a run
        # ends (a following job in another session updates the same row and would block).
        await self.db.commit()
        if new_hash:
            self.runner.opened_state_hash = new_hash
            self.stats["session_state_refreshes"] = int(self.stats.get("session_state_refreshes", 0) or 0) + 1

    # ---------------------------------------------------------------- guards
    def _assert_permitted(self) -> None:
        if not settings.login_scraping_effective:
            raise PermissionError("ALLOW_LOGIN_SCRAPING is false or ENVIRONMENT != chambers; login-session scraping is not permitted here")
        if self.source.state in ("HALTED", "DISABLED"):
            raise PermissionError(f"source is {self.source.state}: {self.source.state_reason}")

    @staticmethod
    def _is_reference_case_surface(page: PageResult) -> bool:
        candidates = [
            page.url or "",
            str((page.metadata or {}).get("requested_url") or ""),
            str((page.metadata or {}).get("final_url") or ""),
        ]
        return any(re.search(r"ReferenceCaseLawSearch", value, flags=re.IGNORECASE) for value in candidates)

    @classmethod
    def _classify_document_type(
        cls,
        *,
        page: PageResult,
        selected_text: str,
        modal_text: Optional[str],
    ) -> tuple[str, Optional[str]]:
        if cls._is_reference_case_surface(page):
            selector_found = bool((page.metadata or {}).get("case_description_selector_found"))
            if not selector_found:
                return "headnote", "case_description_selector_missing"
            if not (modal_text or "").strip():
                return "headnote", "case_description_modal_empty"
        signal = detect_headnotes_only(raw_text=selected_text, raw_html=page.html)
        if signal is not None:
            return "headnote", f"{signal.reason_code}:{signal.signal}"
        return "full_judgment", None

    # ---------------------------------------------------------------- search map
    async def ensure_search_map(self) -> Dict[str, Any]:
        """Specification 3.3: reuse the active, non-stale SearchFormMap; otherwise render
        PLS_SEARCH_URL (a block/login/verification verdict stops here) and introspect the form."""
        m = await active_map(self.db, SOURCE_NAME)
        if m is not None and not m.stale:
            return map_as_dict(m)

        async def op(browser: Browser) -> PageResult:
            page = await browser.goto(settings.PLS_SEARCH_URL)
            raise_for_verdict(page)
            return page

        page = await self.runner.run(op)
        m = await map_search_form(self.db, self.source, page.html, local_engine=self.local_engine)
        # A stale map is a page-shape failure, not a terminal frontier outcome.  Once a
        # fresh map is saved, retry the rows that were paused for remapping.
        await self.db.execute(
            update(CrawlFrontier)
            .where(CrawlFrontier.source_name == SOURCE_NAME, CrawlFrontier.status == "stale")
            .values(status="pending", last_error=None)
        )
        await self.db.flush()
        return map_as_dict(m)

    # ---------------------------------------------------------------- one result page
    async def fetch_results(self, search_map: Dict[str, Any], values: Dict[str, str]) -> PageResult:
        async def op(browser: Browser) -> PageResult:
            await browser.goto(settings.PLS_SEARCH_URL)
            page = await browser.submit_search(search_map, values)
            raise_for_verdict(page)
            return page

        await self._charge_page()
        return await self.runner.run(op)

    async def fetch_detail(self, url: str) -> PageResult:
        async def op(browser: Browser) -> PageResult:
            capture_case_description_modal = bool(re.search(r"ReferenceCaseLawSearch", url or "", flags=re.IGNORECASE))
            page = await browser.goto(url, capture_case_description_modal=capture_case_description_modal)
            raise_for_verdict(page)
            return page

        await self._charge_page()
        return await self.runner.run(op)

    async def download(self, url: str) -> bytes:
        async def op(browser: Browser) -> bytes:
            return await browser.download(url)

        return await self.runner.run(op)

    async def preserve_and_extract(self, page: PageResult, route: Dict[str, Any], row: Dict[str, Any]) -> str:
        """Raw-first: provenance → staging → extraction. Returns 'staged' or 'duplicate'."""
        html = page.html
        html_prov = await record_provenance(
            self.db,
            source=self.source,
            url=page.url,
            content=html.encode("utf-8"),
            content_kind="html",
            route=route,
            http_status=page.status,
        )
        text = clean_html(html)
        modal_text = strip_leading_judgment_chrome((page.metadata or {}).get("case_description_modal_text"))
        if modal_text:
            text = modal_text
        document_type, document_type_reason = self._classify_document_type(
            page=page,
            selected_text=text,
            modal_text=modal_text,
        )
        content_prov = html_prov
        if modal_text and document_type == "full_judgment":
            # Use modal body bytes as identity when they are the selected full judgment text.
            content_prov = await record_provenance(
                self.db,
                source=self.source,
                url=page.url,
                content=modal_text.encode("utf-8"),
                content_kind="text",
                route=route,
                http_status=page.status,
                document_kind="case_description_modal",
                parent=html_prov,
            )
        modal_judges = extract_before_jj_judge_names(modal_text) if modal_text else []
        pdf_prov = None
        ocr = False
        if row.get("pdf_url"):
            try:
                pdf_bytes = await self.download(row["pdf_url"])
                if pdf_bytes[:4] == b"%PDF":
                    pdf_prov = await record_provenance(self.db, source=self.source, url=row["pdf_url"], content=pdf_bytes, content_kind="pdf", route=route, is_original_document=True, document_kind="original_pdf", parent=html_prov)
                    from scraper.fetchers import pdf_text_with_ocr

                    pdf_text, ocr = pdf_text_with_ocr(pdf_bytes)
                    if len(pdf_text) > len(text):
                        text = pdf_text
            except (ExplicitBlock, VerificationRequired, LoginRequired, BrowserDisconnected):
                raise
            except Exception as exc:
                logger.warning("PDF download failed for %s: %s", row.get("pdf_url"), exc)
        text = strip_leading_judgment_chrome(text)
        staging = await stage_judgment(self.db, source=self.source, prov=content_prov, raw_html=html, raw_text=text, url=page.url, route=route, job_id=self.job_id, pdf_prov=pdf_prov, ocr_applied=ocr)
        if staging.status != "pending" or staging.reconciled_json is not None:
            # Already seen via another route: provenance kept the new route; nothing to re-extract.
            routes = list(staging.route_json.get("routes", [])) if isinstance(staging.route_json, dict) else []
            if route not in routes:
                routes.append(route)
                staging.route_json = {**(staging.route_json or {}), "routes": routes}
            self.stats["duplicates"] += 1
            return "duplicate"
        await self.db.flush()
        extractor = HybridExtractor(self.db, self.source, local=self.local_engine, provenance_id=content_prov.id, staging_id=staging.id)
        deterministic_only = self._is_reference_case_surface(page)
        outcome = await extractor.extract_judgment(
            html=html,
            text=text,
            source_meta={"citation": row.get("citation"), "title": row.get("title"), "court": row.get("court"), "url": page.url},
            content_hash=content_prov.content_hash,
            deterministic_only=deterministic_only,
        )
        reconciled = dict(outcome.data or {})
        if modal_judges:
            existing = [str(j) for j in (reconciled.get("judge_names") or []) if j]
            seen = {name.lower() for name in existing}
            for judge_name in modal_judges:
                if judge_name.lower() not in seen:
                    existing.append(judge_name)
                    seen.add(judge_name.lower())
            reconciled["judge_names"] = existing
        reconciled["document_type"] = document_type
        if document_type_reason:
            reconciled["document_type_reason"] = document_type_reason
        staging.deterministic_json = _slim(outcome.deterministic_json)
        staging.ai_json = _slim(outcome.ai_json)
        staging.reconciled_json = _slim(reconciled)
        staging.extraction_engine = outcome.engine
        staging.confidence_score = outcome.confidence
        validation_errors = outcome.errors + [c.get("reason", "") for c in outcome.conflicts]
        if document_type == "headnote":
            validation_errors.append("headnote_only: not eligible for full_judgment promotion")
        staging.validation_errors = validation_errors
        staging.extracted_citation = (reconciled.get("citations") or [None])[0]
        staging.extracted_title = reconciled.get("case_title")
        staging.extracted_court = reconciled.get("court")
        staging.extracted_year = reconciled.get("year")
        staging.status = "quarantined" if outcome.quarantine else "extracted"
        staging.quarantine_reason = outcome.quarantine_reason
        await self.db.flush()
        self.stats["staged"] += 1
        return "staged"

    async def process_result_page(self, page: PageResult, search_map: Dict[str, Any], frontier: CrawlFrontier, start_index: int = 0) -> Dict[str, Any]:
        extractor = HybridExtractor(self.db, self.source, local=self.local_engine)
        outcome = await extractor.extract_result_rows(html=page.html, search_map=search_map, base_url=page.url)
        rows = outcome.data.get("result_rows") or []
        m = await active_map(self.db, SOURCE_NAME)
        parse_ok = bool(rows) or "no record" in page.html.lower() or "no result" in page.html.lower() or "0 results" in page.html.lower()
        if m is not None:
            went_stale = await record_parse_result(self.db, m, ok=parse_ok, source_name=SOURCE_NAME)
            if went_stale:
                frontier.status = "stale"
                frontier.last_error = "search map stale after consecutive result parse failures; remap required"
                raise SearchMapStale(frontier.last_error)
        self.stats["pages"] += 1
        self.stats["rows"] += len(rows)
        for idx, row in enumerate(rows):
            if idx < start_index:
                continue
            route = {"tier": frontier.tier, "query": frontier.query_json, "cursor": dict(frontier.cursor_json), "row_index": idx, "slot": self.runner.browser.slot_number if self.runner.browser else None}
            detail_url = row.get("detail_url") or row.get("pdf_url")
            if not detail_url:
                continue
            detail = await self.fetch_detail(detail_url)
            await self.preserve_and_extract(detail, route, row)
            frontier.cursor_json = {**frontier.cursor_json, "row_index": idx + 1}
            await self.db.flush()
        return {"rows": rows, "next_page": outcome.data.get("next_page")}

    # ---------------------------------------------------------------- tiers
    async def run_tier1(self, frontier: CrawlFrontier, search_map: Dict[str, Any], max_probes: int) -> None:
        rep, year = frontier.query_json["reporter"], int(frontier.query_json["year"])
        cov = (await self.db.execute(select(CrawlCoverage).where(CrawlCoverage.source_name == SOURCE_NAME, CrawlCoverage.reporter == rep, CrawlCoverage.year == year))).scalars().first()
        if cov is None:
            cov = CrawlCoverage(source_name=SOURCE_NAME, reporter=rep, year=year)
            self.db.add(cov)
            await self.db.flush()
        if cov.volume_state == "closed":
            frontier.status = "done"
            return
        page_no = int(frontier.cursor_json.get("page_no") or cov.next_page_to_probe or 1)
        for _ in range(max_probes):
            values = build_values(search_map, frontier.query_json, {"page_no": page_no})
            reason = unmapped_query_reason(search_map, frontier.query_json, {"page_no": page_no})
            if reason:
                frontier.status = "retired"
                frontier.last_error = reason
                return
            page = await self.fetch_results(search_map, values)
            self.stats["queries"] += 1
            result = await self.process_result_page(page, search_map, frontier, start_index=int(frontier.cursor_json.get("row_index", 0)) if frontier.cursor_json.get("page_no") == page_no else 0)
            if result["rows"]:
                cov.consecutive_misses = 0
                cov.highest_page_seen = max(cov.highest_page_seen, page_no)
                cov.judgments_found += len(result["rows"])
                frontier.yield_count += len(result["rows"])
                routes = dict(cov.routes_json or {})
                routes["tier1"] = routes.get("tier1", 0) + len(result["rows"])
                cov.routes_json = routes
            else:
                cov.consecutive_misses += 1
                self.stats["misses"] += 1
            page_no += 1
            cov.next_page_to_probe = page_no
            frontier.cursor_json = {"page_no": page_no, "row_index": 0}
            frontier.last_run_at = datetime.now(timezone.utc)
            if cov.consecutive_misses >= settings.VOLUME_END_GAP:
                cov.volume_state = "closed"
                cov.closed_at = datetime.now(timezone.utc)
                frontier.status = "done"
                self.stats["volumes_closed"] += 1
                await notify(self.db, level="info", code="VOLUME_CLOSED", message=f"{rep} {year} closed after {cov.consecutive_misses} consecutive misses (highest page {cov.highest_page_seen})", source_name=SOURCE_NAME)
                break
            await self.db.flush()

    async def run_paged_query(self, frontier: CrawlFrontier, search_map: Dict[str, Any], max_pages: int) -> None:
        """Tiers 2–4: a query with ordinary pagination; cursor = {page, row_index, next_url}.
        A row that stopped mid-way resumes from its saved next_url (specification 3.5: nothing
        ever restarts from page one); only a fresh row submits the query."""
        page_idx = int(frontier.cursor_json.get("page") or 1)
        values = build_values(search_map, frontier.query_json, frontier.cursor_json)
        reason = unmapped_query_reason(search_map, frontier.query_json, frontier.cursor_json)
        if reason:
            frontier.status = "retired"
            frontier.last_error = reason
            return
        pages_done = 0
        saved_next = frontier.cursor_json.get("next_url") if page_idx > 1 else None
        if saved_next:
            page = await self.fetch_detail(saved_next)
        else:
            page = await self.fetch_results(search_map, values)
        self.stats["queries"] += 1
        while True:
            result = await self.process_result_page(page, search_map, frontier, start_index=int(frontier.cursor_json.get("row_index", 0)))
            frontier.yield_count += len(result["rows"])
            pages_done += 1
            frontier.last_run_at = datetime.now(timezone.utc)
            nxt = result.get("next_page")
            if not nxt or pages_done >= max_pages:
                if not nxt:
                    if frontier.tier == 4:
                        frontier.status = "pending"
                        frontier.cursor_json = {"page": 1, "row_index": 0}
                        frontier.next_run_at = datetime.now(timezone.utc) + timedelta(days=1)
                    elif frontier.tier == 3:
                        if frontier.yield_count == 0:
                            frontier.attempts += 1
                            if frontier.attempts >= TIER3_RETIRE_AFTER:
                                frontier.status = "retired"
                            else:
                                frontier.status = "pending"
                                frontier.next_run_at = datetime.now(timezone.utc) + timedelta(days=7)
                        else:
                            frontier.status = "done"
                            frontier.priority = max(1, 80 - min(frontier.yield_count, 79))
                    else:
                        frontier.status = "done"
                else:
                    frontier.cursor_json = {"page": page_idx + 1, "row_index": 0, "next_url": nxt}
                break
            page_idx += 1
            frontier.cursor_json = {"page": page_idx, "row_index": 0, "next_url": nxt}
            await self.db.flush()
            page = await self.fetch_detail(nxt)
            self.stats["pages"] += 0

    # ---------------------------------------------------------------- main loop
    async def run(self, *, max_queries: int = 20, max_probes_per_volume: int = 60) -> Dict[str, Any]:
        """Specification 3.7: assert permitted -> acquire lock -> current ACTIVE slot (none: pause
        the source) -> seed frontier -> ensure search map -> take up to 20 due frontier rows
        ordered by tier, priority, created_at -> run each."""
        self._assert_permitted()
        lock = SessionLock(SOURCE_NAME, self.redis_client)
        try:
            await lock.acquire()
        except SessionLockHeld:
            logger.warning("refusing to start: another login-session worker holds the lock")
            await lock.release()  # closes the client the lock opened for itself
            raise
        try:
            self._session_lock = lock
            slot = await self.manager.current_slot()
            if slot is None:
                await self.manager.pause_source("no ACTIVE slot: human login required")
                self.stats["paused"] = True
                return self.stats
            self.stats["slot"] = slot.slot_number
            await seed_frontier(self.db, self.source)
            search_map = await self.ensure_search_map()
            now = datetime.now(timezone.utc)
            q = (
                select(CrawlFrontier)
                .where(CrawlFrontier.source_name == SOURCE_NAME, CrawlFrontier.status.in_(["pending", "in_progress"]))
                .where((CrawlFrontier.next_run_at.is_(None)) | (CrawlFrontier.next_run_at <= now))
                .order_by(CrawlFrontier.tier.asc(), CrawlFrontier.priority.asc(), CrawlFrontier.created_at.asc())
                .limit(max_queries)
            )
            frontier_rows = (await self.db.execute(q)).scalars().all()
            for fr in frontier_rows:
                fr.status = "in_progress"
                fr.slot_number = self.runner.browser.slot_number if self.runner.browser else slot.slot_number
                await self.db.flush()
                try:
                    if fr.tier == 1:
                        await self.run_tier1(fr, search_map, max_probes_per_volume)
                    else:
                        await self.run_paged_query(fr, search_map, max_pages=10)
                    if fr.status not in {"stale", "retired"}:
                        fr.last_error = None
                except SearchFormSubmissionError as exc:
                    m = await active_map(self.db, SOURCE_NAME)
                    if m is not None:
                        await mark_map_stale(self.db, m, source_name=SOURCE_NAME, reason=str(exc))
                    fr.status = "stale"
                    fr.last_error = f"search form submission rejected; remap required: {exc}"
                    await self.db.flush()
                    return self.stats
                except ExplicitBlock as exc:
                    fr.status = "pending"
                    fr.last_error = f"halted: {exc}"
                    self.stats["halted"] = True
                    await self.db.flush()
                    return self.stats
                except (LoginRequired, VerificationRequired, NoActiveSlot) as exc:
                    fr.status = "pending"
                    fr.last_error = f"paused: {exc}"
                    self.stats["paused"] = True
                    await self.db.flush()
                    return self.stats
                except BrowserDisconnected as exc:
                    fr.status = "pending"
                    fr.last_error = f"disconnected: {exc}"
                    self.stats["paused"] = True
                    await self.db.flush()
                    return self.stats
                except PacingBudgetExceeded as exc:
                    fr.status = "pending"
                    fr.last_error = f"pacing: {exc}"
                    self.stats["pacing_paused"] = True
                    logger.info("PakistanLawSite pacing budget reached: %s; the source stays ACTIVE and Beat resumes it later", exc)
                    await self.db.flush()
                    return self.stats
                except SearchMapStale:
                    await self.db.flush()
                    return self.stats
                await lock.refresh()
                await self.db.flush()
            self.source.last_scraped_at = datetime.now(timezone.utc)
            self.source.last_success_at = self.source.last_scraped_at
            await self.db.flush()
            return self.stats
        finally:
            self._session_lock = None
            try:
                await self._persist_live_session()
            except Exception as exc:
                logger.warning("PakistanLawSite: could not persist the live session at run end: %s", exc)
            await self.runner.close()
            await lock.release()


def _slim(d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if d is None:
        return None
    out = dict(d)
    for k in ("full_text_candidate", "full_text"):
        if isinstance(out.get(k), str) and len(out[k]) > 2000:
            out[k] = out[k][:2000] + f"…[{len(d[k])} chars in raw_text]"
    return out


async def scrape_pakistanlawsite(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    pipeline = PakistanLawSitePipeline(db, source, **kwargs)
    return await pipeline.run()
