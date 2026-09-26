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
import time
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
    SessionLock,
    SessionLockHeld,
    merge_source_config,
    SessionManager,
    playwright_browser_factory,
    raise_for_verdict,
)
from scraper.config import KNOWN_REPORTERS, settings
from scraper.extractors.hybrid_extractor import HybridExtractor
from scraper.pls_navigation import open_citation_search_for_harvest
from scraper.extractors.judgment_guards import (
    detect_headnotes_only,
    extract_before_jj_judge_names,
    judgment_is_full_ready,
    strip_leading_judgment_chrome,
)
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.fetchers import record_provenance, stage_judgment
from scraper.harvest_mode import get_harvest_mode, login_pacing_profile
from scraper.models import Citation, CrawlCoverage, CrawlFrontier, Judgment, ScraperJob, ScraperSource, ScraperStaging, StatuteSection, Statute
from scraper.notify import notify
from scraper.parsers.citation_extractor import normalise_citation
from scraper.parsers.text_cleaner import clean_html
from scraper.security import ExplicitBlock, VerificationRequired
from scraper.tasks.search_map import active_map, map_as_dict, map_search_form, mark_map_stale, record_parse_result

logger = logging.getLogger(__name__)

SOURCE_NAME = "PakistanLawSite"
TIER3_RETIRE_AFTER = 3
TIER4_HIGH_YIELD_TERMS = 10
DEFAULT_REPORTER_SHARD_TITLES = ("PLD", "SCMR", "CLC", "PCrLJ", "PTD", "PLC", "CLD", "YLR", "MLD")
# Confirmed absolute-seek modes: only these may keep a non-zero snapshot start_row.
# dom_absolute = primary live path; offset-th <tr> confirmed in DOM (New Bot / droplet).
# datatable = optional fallback when DataTables is present and page.info().start lands (#101).
CONFIRMED_CITATION_GRID_SEEK_MODES = frozenset({"datatable", "dom_absolute"})


_PCRLJ_RE = re.compile(r"\bP\s*CR\.?\s*L\.?\s*J\b", re.IGNORECASE)
_REPORTER_BY_UPPER: Dict[str, str] = {}
for _reporter in KNOWN_REPORTERS:
    _REPORTER_BY_UPPER.setdefault(_reporter.upper(), _reporter)


def reporter_from_citation(citation: str) -> str:
    """Reporter title of a citation in either order: "PLD 2024 SC 1" and "2024 CLC 1234" both resolve.

    Every reporter except PLD is cited year-first, so matching only the leading token (the old rule)
    returned the year for SCMR/CLC/YLR/MLD/PCrLJ rows and the reporter shards dropped them all.
    Returns "" when no known reporter title is present."""
    raw = (citation or "").strip()
    if not raw:
        return ""
    candidates = [raw]
    normalized = normalise_citation(raw)
    if normalized and normalized != raw:
        candidates.insert(0, normalized)
    for text in candidates:
        if _PCRLJ_RE.search(text):
            return "PCrLJ"
        for token in re.findall(r"[A-Za-z][A-Za-z.]*", text):
            key = token.replace(".", "").upper()
            if key in _REPORTER_BY_UPPER:
                return _REPORTER_BY_UPPER[key]
    return ""


def split_reporter_shards(reporters: Optional[List[str]] = None) -> tuple[List[str], List[str]]:
    names = [str(item).strip() for item in (reporters or []) if str(item).strip()]
    if not names:
        names = list(DEFAULT_REPORTER_SHARD_TITLES)
    midpoint = (len(names) + 1) // 2
    return names[:midpoint], names[midpoint:]


def citation_grid_cursor_key(reporter_shard: Optional[int]) -> str:
    if reporter_shard in (0, 1):
        return f"citation_grid_cursor_shard_{reporter_shard}"
    return "citation_grid_cursor"


def pacing_key(slot_number: int) -> str:
    """config_json key of one slot's page counters. The site's quota is per account (per login), so
    the budgets are kept per slot, not per source."""
    return f"pacing_slot_{int(slot_number or 0)}"


def _aggregate_legacy_pacing(cfg: Dict[str, Any], *, hour_key: str, day_key: str) -> Dict[str, Any]:
    """Sum per-slot pacing counters from an earlier release for the current hour/day only."""
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


class PacingBudgetExceeded(RuntimeError):
    """PAGES_PER_HOUR / PAGES_PER_DAY spent; the run pauses and Beat resumes it later."""


# Hard ceiling on consecutive citation-grid windows inside one job (the time budget normally ends
# the loop first). A 20k-row grid at 400 rows per window is 50 windows.
MAX_CITATION_GRID_WINDOWS_PER_RUN = 500
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
            elif "keyword" in fields:
                values["keyword"] = f"{query['year']} {query['reporter']} {cursor['page_no']}"
            elif "citation_no" in fields:
                values["citation_no"] = str(cursor["page_no"])
        if "reporter" not in fields and "keyword" in fields and "page_no" not in cursor:
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
    surface = search_map.get("surface") or (search_map.get("limits") or {}).get("surface")
    if surface == "grid_surface_no_query_form":
        return "CitationSearch surface is grid_surface_no_query_form; no query form is available"
    values = build_values(search_map, query, cursor)
    fields = search_map.get("fields") or {}
    if "reporter" in query:
        if {"reporter", "year"}.issubset(fields) and ({"page", "citation", "citation_no"} & set(fields)):
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


def is_grid_surface_without_query_form(search_map: Dict[str, Any]) -> bool:
    surface = search_map.get("surface") or (search_map.get("limits") or {}).get("surface")
    return surface == "grid_surface_no_query_form"


def is_unharvestable_search_surface(search_map: Dict[str, Any]) -> bool:
    """Surfaces that cannot drive tier frontiers: mark stale, never retire rows."""
    surface = search_map.get("surface") or (search_map.get("limits") or {}).get("surface")
    if surface in ("grid_surface_no_query_form", "no_query_form"):
        return True
    fields = search_map.get("fields") or {}
    usable = [key for key in fields if key != "_all"]
    return not usable and not (fields.get("_all") or [])


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
        reporter_shard: Optional[int] = None,
    ):
        self.db = db
        self.source = source
        self.manager = SessionManager(db, source)
        self.runner = ContinuityRunner(self.manager, browser_factory, sleep=sleep)
        self.local_engine = local_engine if local_engine is not None else LocalScrapeGraphEngine()
        self.redis_client = redis_client
        self.job_id = job_id
        self.sleep = sleep
        self.reporter_shard = reporter_shard if reporter_shard in (0, 1) else None
        self.reporter_shard_reporters: List[str] = []
        self._other_shard_reporters: List[str] = []
        self._session_lock: Optional[SessionLock] = None
        self._slot_lock: Optional[SessionLock] = None
        self._surface_page: Optional[PageResult] = None
        self._surface_page_start_row: Optional[int] = None
        self.stats = {"queries": 0, "pages": 0, "rows": 0, "staged": 0, "duplicates": 0, "misses": 0, "url_less_skips": 0, "known_citation_skips": 0, "staged_citation_skips": 0, "reporter_skips": 0, "volumes_closed": 0, "halted": False, "paused": False, "pacing_paused": False, "pages_charged": 0, "citation_grid_windows": 0}
        self.harvest_mode = "updates"
        self.pacing_profile = login_pacing_profile("updates")

    # ---------------------------------------------------------------- pacing (LOGIN_DELAY_*, PAGES_PER_*)
    async def _pacing_slot_number(self) -> int:
        browser = self.runner.browser
        if browser is not None:
            return int(getattr(browser, "slot_number", 0) or 0)
        if self.runner.preferred_slot_number:
            return int(self.runner.preferred_slot_number)
        current = await self.manager.current_slot()
        return int(current.slot_number) if current is not None else 0

    async def _charge_page(self) -> None:
        """Count one login-session page against the hourly and daily budgets of the slot in use,
        then pace. Counters are per slot (the site's quota is per account) and are merged into the
        source row atomically, so a shard on the other slot never overwrites them."""
        if self._session_lock is not None:
            await self._session_lock.refresh()
        if self._slot_lock is not None:
            await self._slot_lock.refresh()
        now = datetime.now(timezone.utc)
        key = pacing_key(await self._pacing_slot_number())
        cfg = dict(self.source.config_json or {})
        pacing = dict(cfg.get(key) or {})
        hour_key = now.strftime("%Y-%m-%dT%H")
        day_key = now.strftime("%Y-%m-%d")
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
        pages_per_day = int(self.pacing_profile["pages_per_day"])
        pages_per_hour = int(self.pacing_profile["pages_per_hour"])
        if pacing["day_pages"] > pages_per_day:
            raise PacingBudgetExceeded(f"PAGES_PER_DAY={pages_per_day} spent for {day_key} on {key}")
        if pacing["hour_pages"] > pages_per_hour:
            raise PacingBudgetExceeded(f"PAGES_PER_HOUR={pages_per_hour} spent for {hour_key} on {key}")
        await self.sleep(random.uniform(float(self.pacing_profile["login_delay_min"]), float(self.pacing_profile["login_delay_max"])))

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

    def _bind_reporter_shard(self) -> None:
        left, right = split_reporter_shards(settings.subscribed_reporters)
        if self.reporter_shard == 0:
            self.reporter_shard_reporters, self._other_shard_reporters = left, right
        elif self.reporter_shard == 1:
            self.reporter_shard_reporters, self._other_shard_reporters = right, left
        else:
            self.reporter_shard_reporters, self._other_shard_reporters = [], []
        self.stats["reporter_shard"] = self.reporter_shard
        self.stats["reporter_shard_titles"] = list(self.reporter_shard_reporters)
        if self.reporter_shard is not None:
            # Shard 0 is the catch-all: it also takes rows whose reporter is unknown or not in the
            # subscribed list, so no row of the grid is left to nobody.
            self.stats["reporter_shard_catch_all"] = self.reporter_shard == 0
            self.runner.preferred_slot_number = self.reporter_shard + 1
            # Two shards run at the same time; they must never share one slot's cookies, or the site
            # ends one of the two sessions ("one login per account").
            self.runner.exclusive_slot = True

    def _row_in_shard(self, reporter: str) -> bool:
        if self.reporter_shard is None:
            return True
        if self.reporter_shard == 1:
            return bool(reporter) and reporter in self.reporter_shard_reporters
        return not (reporter and reporter in self._other_shard_reporters)

    @staticmethod
    def _has_queryable_search_fields(search_map: Dict[str, Any]) -> bool:
        fields = search_map.get("fields") or {}
        return any(role in fields for role in ("reporter", "year", "page", "keyword", "statute", "section", "citation_no"))

    @staticmethod
    def _is_citation_grid_surface(page: PageResult) -> bool:
        marker = str((page.metadata or {}).get("content_guard") or "")
        if marker in ("archivedpatientGrid_compact", "archivedpatientGrid_snapshot_failed"):
            return True
        low = (page.html or "").lower()
        return "id=\"archivedpatientgrid\"" in low or "id='archivedpatientgrid'" in low

    @staticmethod
    def _uses_compact_citation_grid_columns(page: PageResult) -> bool:
        marker = str((page.metadata or {}).get("content_guard") or "")
        return marker in ("archivedpatientGrid_compact", "archivedpatientGrid_snapshot_failed")

    @classmethod
    def _is_citation_grid_map(cls, search_map: Dict[str, Any]) -> bool:
        row_sel = str(((search_map.get("result_layout") or {}).get("row_selector") or "")).lower()
        if "archivedpatientgrid" not in row_sel:
            return False
        return not cls._has_queryable_search_fields(search_map)

    @staticmethod
    def _with_compact_citation_grid_columns(search_map: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(search_map or {})
        layout = dict(normalized.get("result_layout") or {})
        columns = dict(layout.get("columns") or {})
        columns["citation"] = 0
        columns["title"] = 1
        columns["court"] = 2
        layout["columns"] = columns
        normalized["result_layout"] = layout
        return normalized

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
    async def ensure_search_map(self, *, archived_grid_start_row: int = 0) -> Dict[str, Any]:
        """Render the search surface once and map it. The rendered page is kept so the first
        citation-grid window can reuse it instead of loading the 10-16 MB CitationSearch DOM twice."""
        start_row = max(0, int(archived_grid_start_row or 0))

        async def op(browser: Browser) -> PageResult:
            return await open_citation_search_for_harvest(browser, archived_grid_start_row=start_row)

        page = await self.runner.run(op)
        self._surface_page = page
        self._surface_page_start_row = start_row
        m = await active_map(self.db, SOURCE_NAME)
        grid_surface = self._is_citation_grid_surface(page)
        if m is not None and (not m.stale or grid_surface):
            cached = map_as_dict(m)
            if grid_surface:
                if self._is_citation_grid_map(cached):
                    if m.stale:
                        # Empty or bounced windows are not the map's fault: the grid is the surface.
                        m.stale = False
                        m.consecutive_parse_failures = 0
                        await self.db.flush()
                        logger.info("PakistanLawSite grid map v%s revived: the citation grid is still the surface", m.map_version)
                    if self._uses_compact_citation_grid_columns(page):
                        return self._with_compact_citation_grid_columns(cached)
                    return cached
                logger.info("PakistanLawSite surface changed to archivedpatientGrid; remapping search surface")
            elif self._has_queryable_search_fields(cached):
                return cached
        m = await map_search_form(self.db, self.source, page.html, local_engine=self.local_engine)
        if not m.stale:
            await self.db.execute(
                update(CrawlFrontier)
                .where(CrawlFrontier.source_name == SOURCE_NAME, CrawlFrontier.status == "stale")
                .values(status="pending", last_error=None)
            )
        await self.db.flush()
        mapped = map_as_dict(m)
        if grid_surface and self._has_queryable_search_fields(mapped):
            # The full CitationSearch DOM (a failed compact snapshot returns it) carries the filter
            # form beside the grid; mapping those inputs would flip the connector into form mode
            # and every later job would type into fields that mean nothing (23 Sep 2026, map v28).
            m.fields = {}
            layout = dict(m.result_layout or {})
            layout["row_selector"] = layout.get("row_selector") or "#archivedpatientGrid tbody tr"
            m.result_layout = layout
            await self.db.flush()
            logger.info("PakistanLawSite map v%s reduced to the citation grid: the surface is the grid, not a form", m.map_version)
            mapped = map_as_dict(m)
        return mapped

    def _citation_grid_limits(self) -> Dict[str, int]:
        """Per-window caps and the per-job time budget for the current harvest mode."""
        if self.harvest_mode == "backfill":
            max_detail = int(getattr(settings, "BACKFILL_PLS_CITATION_GRID_MAX_DETAIL", 300) or 300)
            scan_window = int(getattr(settings, "BACKFILL_PLS_CITATION_GRID_SCAN_WINDOW", 600) or 600)
            run_minutes = int(getattr(settings, "BACKFILL_PLS_RUN_MAX_MINUTES", 50) or 0)
        else:
            max_detail = int(getattr(settings, "PLS_CITATION_GRID_MAX_DETAIL", 120) or 120)
            scan_window = int(getattr(settings, "PLS_CITATION_GRID_SCAN_WINDOW", 200) or 200)
            run_minutes = int(getattr(settings, "PLS_RUN_MAX_MINUTES", 0) or 0)
        max_detail = max(1, max_detail)
        scan_window = max(max_detail, scan_window)
        return {"max_detail": max_detail, "scan_window": scan_window, "run_minutes": max(0, run_minutes)}

    def _citation_grid_cursor(self) -> tuple[str, Dict[str, Any], int]:
        cfg = dict(self.source.config_json or {})
        cursor_key = citation_grid_cursor_key(self.reporter_shard)
        cursor = dict(cfg.get(cursor_key) or cfg.get("citation_grid_cursor") or {})
        try:
            row_offset = int(cursor.get("row_offset", 0) or 0)
        except Exception:
            row_offset = 0
        return cursor_key, cursor, max(0, row_offset)

    async def run_citation_grid_surface(self, search_map: Dict[str, Any]) -> None:
        """CitationSearch is an authenticated citation table, not a form: walk it window by window.

        One window = one compact snapshot of up to PLS_ARCHIVED_GRID_MAX_ROWS rows at the cursor,
        then detail fetches for the rows not yet in the corpus. In backfill mode the loop keeps
        taking consecutive windows for BACKFILL_PLS_RUN_MAX_MINUTES so the session is busy for the
        whole job instead of stopping after one window and idling until the next Beat kick. Every
        row's progress is committed as it happens, so a worker restart resumes at the same row."""
        limits = self._citation_grid_limits()
        deadline = time.monotonic() + limits["run_minutes"] * 60 if limits["run_minutes"] > 0 else None
        self.stats["surface_mode"] = "citation_grid"
        self.stats["citation_grid_run_minutes"] = limits["run_minutes"]
        windows = 0
        while True:
            outcome = await self._run_citation_grid_window(search_map, limits)
            windows += 1
            self.stats["citation_grid_windows"] = windows
            await self._persist_live_session()
            if deadline is None:
                break
            if outcome["rows"] == 0:
                logger.info("PakistanLawSite citation-grid window returned no rows; ending the run")
                break
            if outcome["wrapped"]:
                logger.info("PakistanLawSite citation-grid cursor wrapped around the grid; ending the run")
                break
            if outcome["cursor_unconfirmed"]:
                # The site could not be asked for the requested row: do not spend the whole budget
                # re-reading the first window again and again.
                break
            if time.monotonic() >= deadline:
                logger.info("PakistanLawSite citation-grid run budget of %s minutes spent after %s windows", limits["run_minutes"], windows)
                break
            if windows >= MAX_CITATION_GRID_WINDOWS_PER_RUN:
                break

    async def _run_citation_grid_window(self, search_map: Dict[str, Any], limits: Dict[str, int]) -> Dict[str, Any]:
        await self._charge_page()
        cursor_key, cursor, row_offset = self._citation_grid_cursor()
        max_detail = limits["max_detail"]
        scan_window = limits["scan_window"]
        result: Dict[str, Any] = {"rows": 0, "processed": 0, "start_offset": row_offset, "next_offset": row_offset, "wrapped": False, "cursor_unconfirmed": False}

        # Reuse the page ensure_search_map() already rendered when it was asked for this exact row.
        page: Optional[PageResult] = None
        if self._surface_page is not None and self._surface_page_start_row == row_offset:
            page = self._surface_page
        self._surface_page = None
        self._surface_page_start_row = None
        if page is None:

            async def op(browser: Browser) -> PageResult:
                loaded = await open_citation_search_for_harvest(browser, archived_grid_start_row=row_offset)
                raise_for_verdict(loaded)
                return loaded

            page = await self.runner.run(op)
        extractor = HybridExtractor(self.db, self.source, local=self.local_engine)
        outcome = await extractor.extract_result_rows(html=page.html, search_map=search_map, base_url=page.url)
        rows = outcome.data.get("result_rows") or []
        m = await active_map(self.db, SOURCE_NAME)
        if m is not None and rows:
            # A window with rows proves the grid map; an empty window (a bounce, the end of the
            # grid) says nothing about it and must not mark it stale.
            await record_parse_result(self.db, m, ok=True, source_name=SOURCE_NAME)
        self.stats["queries"] += 1
        self.stats["pages"] += 1
        self.stats["rows"] += len(rows)
        if not rows:
            self.stats["misses"] += 1
            return result
        row_count = len(rows)
        result["rows"] = row_count
        total_rows_meta = (page.metadata or {}).get("total_rows")
        try:
            total_rows = int(total_rows_meta) if total_rows_meta is not None else None
        except Exception:
            total_rows = None
        if total_rows is None:
            try:
                total_rows = int(cursor.get("last_total_rows"))
            except Exception:
                total_rows = None
        if total_rows is None or total_rows <= 0:
            total_rows = row_count
        if row_offset >= total_rows:
            row_offset = row_offset % total_rows
        start_row_meta = (page.metadata or {}).get("start_row")
        seek_mode = str((page.metadata or {}).get("seek_mode") or "").strip().lower()
        try:
            snapshot_start_row = int(start_row_meta) if start_row_meta is not None else 0
        except Exception:
            snapshot_start_row = 0
        snapshot_start_row = max(0, snapshot_start_row)
        self.stats["citation_grid_seek_mode"] = seek_mode or "none"
        if seek_mode not in CONFIRMED_CITATION_GRID_SEEK_MODES and snapshot_start_row > 0:
            logger.warning(
                "PakistanLawSite citation-grid snapshot reported start_row=%s for seek_mode=%s; normalizing to 0",
                snapshot_start_row,
                seek_mode,
            )
            snapshot_start_row = 0
        window_contains_offset = snapshot_start_row <= row_offset < snapshot_start_row + row_count
        seek_confirmed = seek_mode in CONFIRMED_CITATION_GRID_SEEK_MODES
        if row_offset > 0 and (not seek_confirmed or not window_contains_offset):
            logger.error(
                "PakistanLawSite citation-grid seek failed; refusing to harvest from row 0 or move cursor "
                "row_offset=%s snapshot_start=%s rows=%s seek_mode=%s",
                row_offset,
                snapshot_start_row,
                row_count,
                seek_mode or "none",
            )
            self.stats["citation_grid_seek_failed"] = True
            self.stats["citation_grid_offset"] = row_offset
            self.stats["citation_grid_snapshot_start"] = snapshot_start_row
            self.stats["citation_grid_rows_seen"] = row_count
            result["cursor_unconfirmed"] = True
            return result
        start_offset = row_offset
        start_in_window = row_offset - snapshot_start_row
        remaining_rows_in_window = max(0, row_count - start_in_window)
        remaining_rows_total = max(0, total_rows - start_offset)
        take_cap = min(scan_window, row_count)
        take_count = min(take_cap, remaining_rows_in_window, remaining_rows_total)
        cfg = dict(self.source.config_json or {})
        raw_flush_every = cfg.get("citation_grid_flush_every", getattr(settings, "PLS_CITATION_GRID_FLUSH_EVERY", 1))
        try:
            flush_every = int(raw_flush_every or 1)
        except Exception:
            flush_every = 1
        flush_every = max(1, flush_every)
        selected_indexes = [start_in_window + i for i in range(take_count)]
        self.stats["citation_grid_scan_window"] = scan_window
        self.stats["citation_grid_detail_cap"] = max_detail
        lookup_keys = set()
        for idx in selected_indexes:
            raw = str(rows[idx].get("citation") or "").strip()
            if not raw:
                continue
            lookup_keys.add(raw)
            normalized = normalise_citation(raw)
            if normalized:
                lookup_keys.add(normalized)
        known_citations: set[str] = set()
        full_ready_citations: set[str] = set()
        staged_citations: set[str] = set()
        if lookup_keys:
            existing_judgments = (
                await self.db.execute(
                    select(Judgment.canonical_citation, Judgment.full_text, Judgment.judge_names).where(
                        Judgment.canonical_citation.in_(list(lookup_keys))
                    )
                )
            ).all()
            for canonical, full_text, judge_names in existing_judgments:
                key = str(canonical)
                known_citations.add(key)
                if judgment_is_full_ready(full_text, judge_names):
                    full_ready_citations.add(key)
            existing_citations = (
                await self.db.execute(
                    select(Citation.citation_string, Judgment.full_text, Judgment.judge_names)
                    .join(Judgment, Judgment.id == Citation.judgment_id)
                    .where(Citation.citation_string.in_(list(lookup_keys)))
                )
            ).all()
            for citation_string, full_text, judge_names in existing_citations:
                key = str(citation_string)
                known_citations.add(key)
                if judgment_is_full_ready(full_text, judge_names):
                    full_ready_citations.add(key)
            if bool(getattr(settings, "PLS_CITATION_GRID_SKIP_STAGED", True)):
                # Pages already preserved and staged (waiting for promotion, promoted, duplicate or
                # quarantined for review) are not downloaded again when the cursor wraps.
                staged_rows = (
                    await self.db.execute(
                        select(ScraperStaging.extracted_citation).where(
                            ScraperStaging.source_name == SOURCE_NAME,
                            ScraperStaging.extracted_citation.in_(list(lookup_keys)),
                            ScraperStaging.status.in_(["extracted", "promoted", "duplicate", "quarantined"]),
                        )
                    )
                ).all()
                for (citation_string,) in staged_rows:
                    if citation_string:
                        staged_citations.add(str(citation_string))
        logger.info(
            "PakistanLawSite citation-grid cursor start_offset=%s start_in_window=%s take_count=%s rows=%s total_rows=%s max_detail=%s scan_window=%s flush_every=%s known_full=%s staged=%s seek_mode=%s",
            start_offset,
            start_in_window,
            take_count,
            row_count,
            total_rows,
            max_detail,
            scan_window,
            flush_every,
            len(full_ready_citations),
            len(staged_citations),
            seek_mode or "none",
        )
        staged_before = self.stats["staged"]
        duplicates_before = self.stats["duplicates"]
        url_less_skips = 0
        known_citation_skips = 0
        staged_citation_skips = 0
        detail_attempts = 0
        processed_rows_total = 0
        self.stats.setdefault("citation_grid_offset", start_offset)
        self.stats["citation_grid_window_offset"] = start_offset
        self.stats["citation_grid_snapshot_start"] = snapshot_start_row
        self.stats["citation_grid_rows_seen"] = row_count
        details_since_flush = 0
        staged_since_flush = 0
        last_committed_offset = start_offset

        def next_offset_after(processed_rows: int) -> int:
            if total_rows <= 0:
                return start_offset + processed_rows
            absolute = start_offset + processed_rows
            if absolute >= total_rows:
                return absolute % total_rows
            return absolute

        async def flush_citation_grid_progress(next_offset: int, *, staged_this_flush: int, details_this_flush: int, processed_rows: int) -> None:
            nonlocal last_committed_offset
            try:
                offset_before = int(cursor.get("row_offset", last_committed_offset) or 0)
            except Exception:
                offset_before = last_committed_offset
            cursor.update(
                {
                    "row_offset": next_offset,
                    "last_start_offset": start_offset,
                    "last_take_count": processed_rows,
                    "last_rows_seen": row_count,
                    "last_total_rows": total_rows,
                    "last_snapshot_start_row": snapshot_start_row,
                    "last_seek_mode": seek_mode or "none",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if total_rows > 0 and next_offset < start_offset:
                cursor["wrapped_at"] = cursor["updated_at"]
                cursor["wraps"] = int(cursor.get("wraps", 0) or 0) + 1
            patch = {cursor_key: cursor}
            if cursor_key == "citation_grid_cursor" or self.reporter_shard is None:
                patch["citation_grid_cursor"] = cursor
            # Atomic top-level merge: the other shard's cursor and counters are never overwritten.
            await merge_source_config(self.db, self.source, patch)
            self.stats["citation_grid_next_offset"] = next_offset
            self.stats["known_citation_skips"] = known_citation_skips
            self.stats["staged_citation_skips"] = staged_citation_skips
            await self._heartbeat_job()
            await self.db.flush()
            await self.db.commit()
            logger.info(
                "PakistanLawSite citation-grid flush offset_before=%s offset_after=%s staged_this_flush=%s details_this_flush=%s processed_rows=%s",
                offset_before,
                next_offset,
                staged_this_flush,
                details_this_flush,
                processed_rows,
            )
            last_committed_offset = next_offset

        for idx, row_idx in enumerate(selected_indexes):
            row = rows[row_idx]
            processed_rows_total = idx + 1
            citation_key = (row.get("citation") or "").strip()
            citation_norm = normalise_citation(citation_key) if citation_key else ""
            if self.reporter_shard is not None:
                row_reporter = reporter_from_citation(citation_key)
                if not self._row_in_shard(row_reporter):
                    self.stats["reporter_skips"] = self.stats.get("reporter_skips", 0) + 1
                    if (idx + 1) % flush_every == 0 or (idx + 1) == len(selected_indexes):
                        next_offset = next_offset_after(idx + 1)
                        await flush_citation_grid_progress(
                            next_offset,
                            staged_this_flush=staged_since_flush,
                            details_this_flush=details_since_flush,
                            processed_rows=idx + 1,
                        )
                        details_since_flush = 0
                        staged_since_flush = 0
                    continue
            is_full_ready = (citation_norm and citation_norm in full_ready_citations) or (
                citation_key and citation_key in full_ready_citations
            )
            is_known = (citation_norm and citation_norm in known_citations) or (
                citation_key and citation_key in known_citations
            )
            is_staged = (citation_norm and citation_norm in staged_citations) or (
                citation_key and citation_key in staged_citations
            )
            if is_full_ready or is_staged:
                if is_full_ready:
                    known_citation_skips += 1
                    self.stats["known_citation_skips"] = known_citation_skips
                else:
                    staged_citation_skips += 1
                    self.stats["staged_citation_skips"] = staged_citation_skips
                    self.stats["duplicates"] += 1
                if (idx + 1) % flush_every == 0 or (idx + 1) == len(selected_indexes):
                    next_offset = next_offset_after(idx + 1)
                    await flush_citation_grid_progress(
                        next_offset,
                        staged_this_flush=staged_since_flush,
                        details_this_flush=details_since_flush,
                        processed_rows=idx + 1,
                    )
                    details_since_flush = 0
                    staged_since_flush = 0
                continue
            if is_known:
                self.stats["incomplete_citation_refetch"] = self.stats.get("incomplete_citation_refetch", 0) + 1
            if detail_attempts >= max_detail:
                processed_rows_total = idx
                next_offset = next_offset_after(idx)
                if idx > 0:
                    await flush_citation_grid_progress(
                        next_offset,
                        staged_this_flush=staged_since_flush,
                        details_this_flush=details_since_flush,
                        processed_rows=idx,
                    )
                logger.info(
                    "PakistanLawSite citation-grid detail cap reached attempts=%s max_detail=%s processed_rows=%s known_skips=%s",
                    detail_attempts,
                    max_detail,
                    idx,
                    known_citation_skips,
                )
                break
            detail_url = row.get("detail_url") or row.get("pdf_url")
            if not detail_url:
                url_less_skips += 1
                self.stats["url_less_skips"] += 1
                logger.warning(
                    "PakistanLawSite citation-grid row missing detail URL; skipping row_index=%s citation=%r title=%r",
                    row_idx,
                    row.get("citation"),
                    row.get("title"),
                )
                if (idx + 1) == len(selected_indexes):
                    next_offset = next_offset_after(idx + 1)
                    await flush_citation_grid_progress(
                        next_offset,
                        staged_this_flush=staged_since_flush,
                        details_this_flush=details_since_flush,
                        processed_rows=idx + 1,
                    )
                    details_since_flush = 0
                    staged_since_flush = 0
                continue
            if idx == 0 or (idx + 1) % 5 == 0 or (idx + 1) == len(selected_indexes):
                logger.info(
                    "PakistanLawSite citation-grid detail progress %s/%s staged=%s duplicates=%s known_skips=%s",
                    idx + 1,
                    len(selected_indexes),
                    self.stats["staged"] - staged_before,
                    self.stats["duplicates"] - duplicates_before,
                    known_citation_skips,
                )
            route = {
                "tier": "citation_grid",
                "query": {"surface": "archivedpatientGrid"},
                "cursor": {
                    "row_index": row_idx,
                    "absolute_row_index": snapshot_start_row + row_idx,
                },
                "row_index": row_idx,
                "absolute_row_index": snapshot_start_row + row_idx,
                "slot": self.runner.browser.slot_number if self.runner.browser else None,
            }
            detail = await self.fetch_detail(detail_url)
            detail_attempts += 1
            result_kind = await self.preserve_and_extract(detail, route, row)
            details_since_flush += 1
            if result_kind == "staged":
                staged_since_flush += 1
            if details_since_flush >= flush_every or (idx + 1) == len(selected_indexes):
                next_offset = next_offset_after(idx + 1)
                await flush_citation_grid_progress(
                    next_offset,
                    staged_this_flush=staged_since_flush,
                    details_this_flush=details_since_flush,
                    processed_rows=idx + 1,
                )
                details_since_flush = 0
                staged_since_flush = 0
        if url_less_skips:
            logger.warning(
                "PakistanLawSite citation-grid skipped %s/%s rows with no detail URL",
                url_less_skips,
                len(selected_indexes),
            )
        if known_citation_skips or staged_citation_skips:
            logger.info(
                "PakistanLawSite citation-grid fast-forwarded %s known full citations and %s already-staged citations out of %s rows",
                known_citation_skips,
                staged_citation_skips,
                len(selected_indexes),
            )
        staged_delta = self.stats["staged"] - staged_before
        if selected_indexes and staged_delta == 0:
            duplicate_delta = self.stats["duplicates"] - duplicates_before
            logger.warning(
                "PakistanLawSite citation-grid produced rows but staged=0 (rows=%s duplicates=%s url_less_skips=%s known_skips=%s)",
                processed_rows_total or len(selected_indexes),
                duplicate_delta,
                url_less_skips,
                known_citation_skips,
            )
            if processed_rows_total and url_less_skips >= processed_rows_total and known_citation_skips == 0:
                raise RuntimeError("citation-grid returned rows but none had a detail URL; refusing false-success run")
        next_offset = next_offset_after(processed_rows_total)
        if last_committed_offset != next_offset:
            await flush_citation_grid_progress(
                next_offset,
                staged_this_flush=0,
                details_this_flush=0,
                processed_rows=processed_rows_total,
            )
        wrapped = bool(total_rows > 0 and next_offset < start_offset)
        result.update({"processed": processed_rows_total, "start_offset": start_offset, "next_offset": next_offset, "wrapped": wrapped})
        logger.info(
            "PakistanLawSite citation-grid cursor window complete start_offset=%s next_offset=%s wrap=%s",
            start_offset,
            next_offset,
            wrapped,
        )
        return result

    # ---------------------------------------------------------------- one result page
    async def mark_frontier_stale_for_unmapped_surface(
        self,
        frontier: CrawlFrontier,
        reason: str,
        *,
        code: str = "SEARCH_MAP_GRID_SURFACE_STALE",
    ) -> None:
        m = await active_map(self.db, SOURCE_NAME)
        if m is not None:
            await mark_map_stale(self.db, m, source_name=SOURCE_NAME, reason=f"{code}: {reason}")
        frontier.status = "stale"
        frontier.last_error = f"{code}: {reason}; remap required"

    async def fetch_results(self, search_map: Dict[str, Any], values: Dict[str, str]) -> PageResult:
        async def op(browser: Browser) -> PageResult:
            await open_citation_search_for_harvest(browser)
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
                if is_unharvestable_search_surface(search_map):
                    surface = search_map.get("surface") or (search_map.get("limits") or {}).get("surface")
                    stale_code = (
                        "SEARCH_MAP_NO_QUERY_FORM_STALE"
                        if surface == "no_query_form"
                        else "SEARCH_MAP_GRID_SURFACE_STALE"
                    )
                    await self.mark_frontier_stale_for_unmapped_surface(frontier, reason, code=stale_code)
                    return
                frontier.status = "retired"
                frontier.last_error = reason or "search map cannot express frontier query"
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
        """Tiers 2–4: a query with ordinary pagination; cursor = (page, row_index)."""
        page_idx = int(frontier.cursor_json.get("page") or 1)
        values = build_values(search_map, frontier.query_json, frontier.cursor_json)
        reason = unmapped_query_reason(search_map, frontier.query_json, frontier.cursor_json)
        if reason:
            if is_unharvestable_search_surface(search_map):
                surface = search_map.get("surface") or (search_map.get("limits") or {}).get("surface")
                stale_code = (
                    "SEARCH_MAP_NO_QUERY_FORM_STALE"
                    if surface == "no_query_form"
                    else "SEARCH_MAP_GRID_SURFACE_STALE"
                )
                await self.mark_frontier_stale_for_unmapped_surface(frontier, reason, code=stale_code)
                return
            frontier.status = "retired"
            frontier.last_error = reason or "search map cannot express frontier query"
            return
        pages_done = 0
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

    async def _recover_slot_after_login_loss(self, exc: Exception) -> bool:
        """Mark the slot dead, run saved-credential sign-in, and return True when ACTIVE again."""
        from scraper.tasks.login_recovery import recover_slot

        slot_no = self.stats.get("slot")
        if slot_no is None and self.runner.browser is not None:
            slot_no = self.runner.browser.slot_number
        if slot_no is None:
            return False
        slot_no = int(slot_no)
        logger.warning(
            "PakistanLawSite login lost on slot %s during harvest (%s); attempting automated re-login",
            slot_no,
            exc,
        )
        await self.manager.mark_needs_human_login(slot_no, f"login lost during harvest: {str(exc)[:300]}")
        await self.db.flush()
        try:
            await self.runner.close()
        except Exception:
            pass
        self.runner.browser = None
        slot = await self.manager.slot(slot_no)
        outcome = await recover_slot(self.db, self.manager, slot)
        self.stats.setdefault("login_recovery", []).append({"slot": slot_no, **outcome})
        await self.db.flush()
        if outcome.get("recovered"):
            await self.db.commit()
            refreshed = await self.manager.slot(slot_no)
            return refreshed.state == "ACTIVE"
        return False

    # ---------------------------------------------------------------- main loop
    async def run(self, *, max_queries: int = 20, max_probes_per_volume: int = 60) -> Dict[str, Any]:
        self._assert_permitted()
        self.harvest_mode = await get_harvest_mode(self.db)
        self.pacing_profile = login_pacing_profile(self.harvest_mode)
        self.stats["harvest_mode"] = self.harvest_mode
        self.stats["pacing_profile"] = {
            "pages_per_hour": self.pacing_profile["pages_per_hour"],
            "pages_per_day": self.pacing_profile["pages_per_day"],
            "login_delay_min": self.pacing_profile["login_delay_min"],
            "login_delay_max": self.pacing_profile["login_delay_max"],
        }
        # One browser per human login: the lock admits at most as many workers as there are ACTIVE
        # slots, whatever the pacing profile targets (two browsers on one login end each other).
        active_slot_count = len([s for s in await self.manager.slots() if s.state == "ACTIVE"])
        max_holders = max(1, min(int(self.pacing_profile.get("login_session_concurrency") or 1), active_slot_count))
        lock = SessionLock(SOURCE_NAME, self.redis_client, max_holders=max_holders)
        try:
            await lock.acquire()
        except SessionLockHeld:
            logger.warning("refusing to start: another login-session worker holds the lock")
            raise
        try:
            self._session_lock = lock
            self._bind_reporter_shard()
            preferred = self.runner.preferred_slot_number
            slot = None
            if preferred:
                candidate = await self.manager.slot(preferred)
                if candidate.state == "ACTIVE":
                    slot = candidate
                elif self.reporter_shard is not None:
                    # A shard runs on its own slot only. Borrowing the other shard's slot would put two
                    # browsers on one login at the same time and the site would end one of them.
                    if await self.manager.current_slot() is None:
                        await self.manager.pause_source("no ACTIVE slot: human login required")
                        self.stats["paused"] = True
                    else:
                        self.stats["skipped"] = "shard_slot_not_active"
                        self.stats["preferred_slot"] = preferred
                        logger.info(
                            "PakistanLawSite shard %s skipped: its slot %s is %s (%s)",
                            self.reporter_shard,
                            preferred,
                            candidate.state,
                            candidate.state_reason,
                        )
                    return self.stats
            if slot is None:
                slot = await self.manager.current_slot()
            if slot is None:
                await self.manager.pause_source("no ACTIVE slot: human login required")
                self.stats["paused"] = True
                return self.stats
            # One browser per login, enforced where it matters: an exclusive lock on the slot itself.
            slot_lock = SessionLock(f"{SOURCE_NAME}:slot{slot.slot_number}", self.redis_client, max_holders=1)
            try:
                await slot_lock.acquire()
            except SessionLockHeld:
                self.stats["skipped"] = "slot_in_use"
                self.stats["slot"] = slot.slot_number
                logger.info(
                    "PakistanLawSite: slot %s is in use by another login-session worker; not opening a second browser on it",
                    slot.slot_number,
                )
                return self.stats
            self._slot_lock = slot_lock
            self.stats["slot"] = slot.slot_number
            grid_start_row = self._citation_grid_cursor()[2]
            search_map = await self.ensure_search_map(archived_grid_start_row=grid_start_row)
            if self._is_citation_grid_map(search_map):
                logger.info(
                    "PakistanLawSite using citation-grid surface mode (archivedpatientGrid) shard=%s titles=%s",
                    self.reporter_shard,
                    self.reporter_shard_reporters or "all",
                )
                # Never mask a login bounce by switching to the other slot: re-login this slot and retry.
                self.runner.exclusive_slot = True
                stopped_clean = True
                login_retries = 0
                while True:
                    try:
                        await self.run_citation_grid_surface(search_map)
                        break
                    except ExplicitBlock as exc:
                        stopped_clean = False
                        self.stats["halted"] = True
                        self.stats["stop_reason"] = f"halted: {exc}"
                        break
                    except LoginRequired as exc:
                        stopped_clean = False
                        if login_retries < 1 and await self._recover_slot_after_login_loss(exc):
                            login_retries += 1
                            self.stats["login_recovery_retries"] = login_retries
                            self._surface_page = None
                            grid_start_row = self._citation_grid_cursor()[2]
                            search_map = await self.ensure_search_map(archived_grid_start_row=grid_start_row)
                            continue
                        self.stats["paused"] = True
                        self.stats["stop_reason"] = f"paused: {exc}"
                        break
                    except (VerificationRequired, NoActiveSlot) as exc:
                        stopped_clean = False
                        self.stats["paused"] = True
                        self.stats["stop_reason"] = f"paused: {exc}"
                        break
                    except BrowserDisconnected as exc:
                        stopped_clean = False
                        self.stats["paused"] = True
                        self.stats["stop_reason"] = f"disconnected: {exc}"
                        break
                    except PacingBudgetExceeded as exc:
                        self.stats["pacing_paused"] = True
                        self.stats["stop_reason"] = f"pacing: {exc}"
                        logger.info("PakistanLawSite pacing budget reached: %s; resuming on the next scheduled run", exc)
                        break
                self.source.last_scraped_at = datetime.now(timezone.utc)
                if stopped_clean:
                    self.source.last_success_at = self.source.last_scraped_at
                await self.db.flush()
                return self.stats
            await seed_frontier(self.db, self.source)
            now = datetime.now(timezone.utc)
            q = (
                select(CrawlFrontier)
                .where(CrawlFrontier.source_name == SOURCE_NAME, CrawlFrontier.status.in_(["pending", "in_progress"]))
                .where((CrawlFrontier.next_run_at.is_(None)) | (CrawlFrontier.next_run_at <= now))
                .order_by(CrawlFrontier.tier.asc(), CrawlFrontier.priority.asc(), CrawlFrontier.created_at.asc())
                .limit(max_queries)
            )
            frontier_rows = (await self.db.execute(q)).scalars().all()
            if self.reporter_shard_reporters:
                frontier_rows = [
                    fr
                    for fr in frontier_rows
                    if not fr.query_json.get("reporter") or fr.query_json.get("reporter") in self.reporter_shard_reporters
                ]
            for fr in frontier_rows:
                fr.status = "in_progress"
                fr.slot_number = self.runner.browser.slot_number if self.runner.browser else slot.slot_number
                await self.db.flush()
                try:
                    if fr.tier == 1:
                        await self.run_tier1(fr, search_map, max_probes_per_volume)
                    else:
                        await self.run_paged_query(fr, search_map, max_pages=10)
                    if fr.status not in ("retired", "stale"):
                        fr.last_error = None
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
                    logger.info("PakistanLawSite pacing budget reached: %s; resuming on the next scheduled run", exc)
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
            if self._slot_lock is not None:
                try:
                    await self._slot_lock.release()
                finally:
                    self._slot_lock = None
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
    reporter_shard = kwargs.pop("reporter_shard", None)
    pipeline = PakistanLawSitePipeline(db, source, reporter_shard=reporter_shard, **kwargs)
    return await pipeline.run()
