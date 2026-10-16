"""
PakistanLawSite search-driven gap harvester: systematic search-bar queries, full result pagination,
gap accounting, and resumable progress in pls_search_harvest_query.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from celery import shared_task
from sqlalchemy import false, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.auth.session_manager import (
    LoginRequired,
    SessionLock,
    SessionLockHeld,
    release_page_result,
)
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.harvest_mode import get_harvest_mode, login_pacing_profile
from scraper.models import (
    BrowserSessionSlot,
    Citation,
    Judgment,
    PlsSearchHarvestQuery,
    ScraperSource,
    ScraperStaging,
)
from scraper.pls_browser_memory import PlsBrowserMemoryHardLimit, PlsBrowserMemoryState, check_rss_limits
from scraper.pls_search_harvest_core import (
    SEARCH_HARVEST_SLOTS,
    build_harvest_form_values,
    CASE_KEY_PREFIX,
    citation_keys_for_row,
    compute_gap_size,
    gap_report_sort_key,
    iter_base_plan_queries,
    make_query_key,
    normalize_query_json,
    parse_total_results_from_html,
    partition_rows_by_known,
    search_harvest_may_mark_done,
    reporters_for_plan,
    slot1_reporter_share,
    cited_citations_in_text,
    is_capped,
    iter_extended_plan_queries,
    page_continuity_gaps,
    split_oversized_query,
    unmapped_harvest_reason,
    years_for_plan,
)
from scraper.tasks.pakistanlawsite import (
    PacingBudgetExceeded,
    PakistanLawSitePipeline,
    SOURCE_NAME,
)
from scraper.tasks.search_map import active_map, map_as_dict

logger = logging.getLogger(__name__)


async def _source_row(db: AsyncSession) -> ScraperSource:
    row = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    if row is None:
        raise RuntimeError(f"source {SOURCE_NAME} not seeded")
    return row


async def load_known_citation_keys(db: AsyncSession, keys: Set[str]) -> Set[str]:
    if not keys:
        return set()
    known: Set[str] = set()
    case_names = {k[len(CASE_KEY_PREFIX):] for k in keys if k.startswith(CASE_KEY_PREFIX)}
    if case_names:
        # a judgment page's CaseName in a promoted or staged (any status) capture: already held
        for model in (Judgment, ScraperStaging):
            name_col = func.upper(func.substring(model.source_url, r"CaseName=(\d{4}[A-Za-z]{1,3}\d{1,6})(?![0-9A-Za-z])"))
            q = select(name_col).where(model.source_name == SOURCE_NAME, name_col.in_(list(case_names)))
            for (name,) in (await db.execute(q)).all():
                if name:
                    known.add(CASE_KEY_PREFIX + str(name))
    key_list = [k for k in keys if not k.startswith(CASE_KEY_PREFIX)]
    if not key_list:
        return known
    for canonical, in (await db.execute(select(Judgment.canonical_citation).where(Judgment.canonical_citation.in_(key_list)))).all():
        if canonical:
            known.add(str(canonical))
    for citation_string, in (
        await db.execute(select(Citation.citation_string).where(Citation.citation_string.in_(key_list)))
    ).all():
        if citation_string:
            known.add(str(citation_string))
    if bool(getattr(settings, "PLS_CITATION_GRID_SKIP_STAGED", True)):
        for extracted, in (
            await db.execute(
                select(ScraperStaging.extracted_citation).where(
                    ScraperStaging.source_name == SOURCE_NAME,
                    ScraperStaging.extracted_citation.in_(key_list),
                    ScraperStaging.status.in_(["extracted", "promoted", "duplicate", "quarantined"]),
                )
            )
        ).all():
            if extracted:
                known.add(str(extracted))
    return known


def _court_options_from_map(search_map: Dict[str, Any]) -> List[str]:
    fields = search_map.get("fields") or {}
    court = fields.get("court") or {}
    opts = [str(o).strip() for o in (court.get("options") or []) if str(o).strip()]
    return opts


def _reporter_options_from_map(search_map: Dict[str, Any]) -> List[str]:
    limits = search_map.get("limits") or {}
    offered = limits.get("reporters_offered") or []
    if offered:
        return [str(x).strip() for x in offered if str(x).strip()]
    fields = search_map.get("fields") or {}
    reporter = fields.get("reporter") or {}
    return [str(o).strip() for o in (reporter.get("options") or []) if str(o).strip()]


async def seed_search_harvest_plan(
    db: AsyncSession,
    *,
    dry_run: bool = False,
    search_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Idempotently insert base (reporter × year) queries."""
    now = datetime.now(timezone.utc)
    current_year = now.year
    earliest = int(settings.PLS_EARLIEST_YEAR or 0) or current_year
    smap = search_map
    if smap is None:
        m = await active_map(db, SOURCE_NAME)
        smap = map_as_dict(m) if m else {}
    reporters = reporters_for_plan(
        subscribed=settings.subscribed_reporters,
        map_reporters=_reporter_options_from_map(smap),
    )
    years = years_for_plan(earliest, current_year)
    existing = {
        r.query_key
        for r in (await db.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME))).scalars().all()
    }
    to_add: List[PlsSearchHarvestQuery] = []
    for query_json in iter_base_plan_queries(reporters, years):
        key = make_query_key(query_json)
        if key in existing:
            continue
        to_add.append(
            PlsSearchHarvestQuery(
                source_name=SOURCE_NAME,
                query_key=key,
                query_json=query_json,
                status="pending",
                priority=100 + (current_year - int(query_json["year"])),
            )
        )
        existing.add(key)
    if dry_run:
        return {"would_insert": len(to_add), "reporters": len(reporters), "years": len(years)}
    for row in to_add:
        db.add(row)
    await db.flush()
    return {"inserted": len(to_add), "reporters": len(reporters), "years": len(years)}


async def seed_extended_plan(
    db: AsyncSession,
    *,
    dry_run: bool = False,
    keywords: Optional[List[str]] = None,
    parties: Optional[List[str]] = None,
    judge_limit: int = 300,
    statute_limit: int = 40,
    sections_per_statute: int = 25,
) -> Dict[str, Any]:
    """Phase 2 families beyond journal x year: court x year, judge x year, statute (per section for the Acts we hold),
    keyword x court, party-word x year. Values come from the court directory, judge and statute tables and the caller
    lists (never invented). Idempotent (query_key unique)."""
    from scraper.models import Court, Judge, Statute, StatuteSection

    now_year = datetime.now(timezone.utc).year
    earliest = int(settings.PLS_EARLIEST_YEAR or 0) or now_year
    years = years_for_plan(earliest, now_year)
    smap = {}
    m = await active_map(db, SOURCE_NAME)
    if m is not None:
        smap = map_as_dict(m)
    courts = _court_options_from_map(smap) or [c for (c,) in (await db.execute(select(Court.name).where(Court.is_active.is_(True)))).all()]
    judges = [n for (n,) in (await db.execute(select(Judge.name).limit(judge_limit))).all() if n]
    statutes = []
    for st_id, st_name in (await db.execute(select(Statute.id, Statute.name).limit(statute_limit))).all():
        secs = [r for (r,) in (await db.execute(select(StatuteSection.section_number).where(StatuteSection.statute_id == st_id).limit(sections_per_statute))).all()]
        statutes.append({"statute": st_name, "sections": secs})
    existing = {r for (r,) in (await db.execute(select(PlsSearchHarvestQuery.query_key).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME))).all()}
    to_add = []
    for q in iter_extended_plan_queries(years=years, courts=courts, judges=judges, statutes=statutes, keywords=keywords or [], parties=parties or []):
        key = make_query_key(q)
        if key in existing:
            continue
        existing.add(key)
        to_add.append(PlsSearchHarvestQuery(source_name=SOURCE_NAME, query_key=key, query_json=q, status="pending", priority=300))
    if not dry_run:
        for row in to_add:
            db.add(row)
        await db.flush()
    return {("would_insert" if dry_run else "inserted"): len(to_add), "courts": len(courts), "judges": len(judges), "statutes": len(statutes), "years": len(years)}


async def seed_snowball(db: AsyncSession, *, limit: int = 2000, dry_run: bool = False) -> Dict[str, Any]:
    """Phase 5: citations that appear inside downloaded PLS judgments but are not in the corpus become citation
    lookups. A round that adds no new query means the snowball has converged."""
    from scraper.models import Judgment

    rows = (await db.execute(select(Judgment.id, Judgment.full_text).where(Judgment.source_name == SOURCE_NAME).order_by(Judgment.promoted_at.desc()).limit(limit))).all()
    cited: Set[str] = set()
    for _id, text_ in rows:
        cited.update(cited_citations_in_text(text_ or ""))
    known = set()
    cl = list(cited)
    for i in range(0, len(cl), 1000):
        known |= await load_known_citation_keys(db, set(cl[i : i + 1000]))
    fresh = sorted(cited - known)
    existing = {r for (r,) in (await db.execute(select(PlsSearchHarvestQuery.query_key).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME))).all()}
    added = 0
    for cit in fresh:
        q = normalize_query_json({"citation": cit})
        key = make_query_key(q)
        if key in existing:
            continue
        existing.add(key)
        added += 1
        if not dry_run:
            db.add(PlsSearchHarvestQuery(source_name=SOURCE_NAME, query_key=key, query_json=q, status="pending", priority=200))
    if not dry_run:
        await db.flush()
    return {"judgments_scanned": len(rows), "distinct_cited": len(cited), "not_in_corpus": len(fresh), "queries_added": added, "converged": added == 0}


async def completeness_report(db: AsyncSession, *, known_sample: int = 200) -> Dict[str, Any]:
    """Phase 6 proof: job-queue state, capped-but-unsplit jobs (never complete), page-continuity holes per reporter
    and year, the site-total comparison, and a random known-citation test against the corpus."""
    from sqlalchemy import func

    from scraper.models import Citation, CrawlFrontier, Judgment, SpotCheck, StatuteSection

    status_counts = {st: int(n) for st, n in (await db.execute(select(PlsSearchHarvestQuery.status, func.count()).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME).group_by(PlsSearchHarvestQuery.status))).all()}
    cap = int(getattr(settings, "PLS_SEARCH_RESULT_CAP", 500) or 500)
    capped_open = int((await db.execute(select(func.count()).select_from(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME, PlsSearchHarvestQuery.status == "done", PlsSearchHarvestQuery.site_total_results >= cap))).scalar() or 0)
    exhausted = int((await db.execute(select(func.count()).select_from(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME, PlsSearchHarvestQuery.status == "failed", PlsSearchHarvestQuery.last_error.ilike("%exhausted%")))).scalar() or 0)
    totals = (await db.execute(select(func.coalesce(func.sum(PlsSearchHarvestQuery.site_total_results), 0), func.coalesce(func.sum(PlsSearchHarvestQuery.rows_known), 0)).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME, PlsSearchHarvestQuery.status == "done"))).one()
    page_rows = (await db.execute(select(Citation.reporter, Citation.year, Citation.page).join(Judgment, Judgment.id == Citation.judgment_id).where(Judgment.source_name == SOURCE_NAME, Citation.is_primary.is_(True), Citation.page.isnot(None)))).all()
    by_ry: Dict[Any, List[int]] = {}
    for rep, yr, pg in page_rows:
        by_ry.setdefault((rep, yr), []).append(pg)
    holes = {f"{rep} {yr}": page_continuity_gaps(pgs) for (rep, yr), pgs in by_ry.items()}
    holes = {k: v for k, v in holes.items() if v}
    sample = (await db.execute(select(Judgment.canonical_citation).where(Judgment.source_name == SOURCE_NAME).order_by(func.random()).limit(known_sample))).scalars().all()
    known = await load_known_citation_keys(db, set(sample)) if sample else set()

    statute_sections = int(
        (await db.execute(select(func.count()).select_from(StatuteSection))).scalar() or 0
    )
    tier2_seeded = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CrawlFrontier)
                .where(CrawlFrontier.source_name == SOURCE_NAME, CrawlFrontier.tier == 2)
            )
        ).scalar()
        or 0
    )
    tier2_missing = max(0, statute_sections - tier2_seeded)
    now = datetime.now(timezone.utc)
    fresh_due = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CrawlFrontier)
                .where(
                    CrawlFrontier.source_name == SOURCE_NAME,
                    CrawlFrontier.tier == 4,
                    CrawlFrontier.status.in_(("pending", "in_progress")),
                    (CrawlFrontier.next_run_at.is_(None)) | (CrawlFrontier.next_run_at <= now),
                )
            )
        ).scalar()
        or 0
    )
    verifier_rows = (
        await db.execute(
            select(SpotCheck.result, func.count())
            .where(
                SpotCheck.kind == "coverage",
                SpotCheck.created_at >= now - timedelta(hours=24),
            )
            .group_by(SpotCheck.result)
        )
    ).all()
    verifier = {result: int(count) for result, count in verifier_rows}
    verifier_checks = sum(verifier.values())
    verifier_misses = int(verifier.get("differs", 0))

    return {
        "queue": status_counts,
        "done_but_capped": capped_open,
        "capped_exhausted_not_complete": exhausted,
        "site_total_done_queries": int(totals[0]),
        "rows_known_done_queries": int(totals[1]),
        "page_continuity_holes": {"reporter_years_with_holes": len(holes), "examples": dict(list(holes.items())[:20])},
        "known_citation_test": {"sampled": len(sample), "found": len(known & set(sample))},
        "tier2_provision_coverage": {
            "statute_sections": statute_sections,
            "frontiers_seeded": tier2_seeded,
            "missing": tier2_missing,
        },
        "fresh_lane": {"due_now": fresh_due},
        "account2_coverage_verifier": {
            "last_24h_checks": verifier_checks,
            "matches": int(verifier.get("match", 0)),
            "misses": verifier_misses,
            "demonstrated": verifier_checks > 0,
        },
        "complete": bool(
            status_counts.get("pending", 0)
            + status_counts.get("in_progress", 0)
            + status_counts.get("failed", 0)
            + capped_open
            == 0
            and not holes
            and tier2_missing == 0
            and fresh_due == 0
            and verifier_checks > 0
            and verifier_misses == 0
        ),
    }


async def reopen_premature_done_queries(
    db: AsyncSession,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Re-queue search harvest jobs marked done after only page 1 with no parsed site total."""
    reopen_reason = "reopened: marked done without known site total and at most one page enumerated"
    stmt = select(PlsSearchHarvestQuery).where(
        PlsSearchHarvestQuery.source_name == SOURCE_NAME,
        PlsSearchHarvestQuery.status == "done",
        PlsSearchHarvestQuery.site_total_results.is_(None),
        func.coalesce(PlsSearchHarvestQuery.pages_enumerated, 0) <= 1,
    )
    rows = (await db.execute(stmt)).scalars().all()
    if dry_run:
        return {
            "would_reopen": len(rows),
            "query_keys": [row.query_key for row in rows[:100]],
        }
    for row in rows:
        row.status = "in_progress"
        row.pages_enumerated = 0
        row.rows_seen = 0
        row.rows_known = 0
        row.rows_new = 0
        row.gap_size = 0
        row.cursor_json = {"page": 1, "row_index": 0}
        row.last_error = reopen_reason
    await db.flush()
    return {"reopened": len(rows)}


async def gap_report_rows(db: AsyncSession, *, limit: int = 500) -> List[Dict[str, Any]]:
    rows = (
        await db.execute(
            select(PlsSearchHarvestQuery)
            .where(PlsSearchHarvestQuery.source_name == SOURCE_NAME)
            .order_by(PlsSearchHarvestQuery.gap_size.desc(), PlsSearchHarvestQuery.site_total_results.desc().nullslast())
            .limit(limit)
        )
    ).scalars().all()
    out: List[Dict[str, Any]] = []
    for row in rows:
        gap = compute_gap_size(row.site_total_results, row.rows_known, row.rows_seen)
        out.append(
            {
                "query_key": row.query_key,
                "status": row.status,
                "query": row.query_json,
                "site_total_results": row.site_total_results,
                "pages_enumerated": row.pages_enumerated,
                "rows_seen": row.rows_seen,
                "rows_known": row.rows_known,
                "rows_new": row.rows_new,
                "gap_size": gap,
                "last_error": row.last_error,
                "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            }
        )
    out.sort(key=gap_report_sort_key)
    return out


def _pick_order(stmt):
    # Priority first (lower is sooner), then the row that has waited longest since its last run: a row whose site
    # total is unknown keeps its planned priority instead of sinking to the back (13 October 2026).
    return stmt.order_by(
        PlsSearchHarvestQuery.priority.asc(),
        PlsSearchHarvestQuery.last_run_at.asc().nulls_first(),
        PlsSearchHarvestQuery.created_at.asc(),
    )


def _reporter_sql():
    return func.upper(func.regexp_replace(func.coalesce(PlsSearchHarvestQuery.query_json["reporter"].astext, ""), r"[\s.]", "", "g"))


def _share_filter(slot: int):
    """Owner ruling 16 Oct 2026: Account 1 owns the complete acquisition ledger; Account 2 owns none."""
    return true() if int(slot) == 1 else false()


async def pick_next_query(
    db: AsyncSession,
    *,
    query_key: Optional[str] = None,
    priority_gaps: bool = False,
    slot: Optional[int] = None,
    take_over: bool = False,
) -> Optional[PlsSearchHarvestQuery]:
    """The next ledger row to work, without taking it (see claim_next_query). `priority_gaps` is accepted from
    ticks queued before 13 October 2026 and no longer changes the order: gap-first put every row with an unknown
    site total (gap 0) last."""
    if query_key:
        return (
            await db.execute(
                select(PlsSearchHarvestQuery).where(
                    PlsSearchHarvestQuery.source_name == SOURCE_NAME,
                    PlsSearchHarvestQuery.query_key == query_key,
                )
            )
        ).scalars().first()
    stmt = select(PlsSearchHarvestQuery).where(
        PlsSearchHarvestQuery.source_name == SOURCE_NAME,
        PlsSearchHarvestQuery.status.in_(("pending", "in_progress")),
    )
    if slot in SEARCH_HARVEST_SLOTS and not take_over:
        stmt = stmt.where(_share_filter(slot))
    return (await db.execute(_pick_order(stmt).limit(1))).scalars().first()


def _query_lock(row_id, redis_client) -> SessionLock:
    return SessionLock(f"{SOURCE_NAME}:search_harvest:query:{row_id}", redis_client, max_holders=1)


async def claim_next_query(
    db: AsyncSession,
    slot: int,
    *,
    redis_client=None,
    take_over: bool = False,
    candidates: int = 25,
) -> Tuple[Optional[PlsSearchHarvestQuery], Optional[SessionLock]]:
    """Take the next ledger row of `slot`'s share (both shares when it has taken over the other slot's) for this
    tick. The candidates are read with SELECT ... FOR UPDATE SKIP LOCKED, so two ticks picking at the same moment
    never read the same row, and the row taken is held by a per-row lock for the whole run, since the run commits
    after every judgment and a row lock alone would end at the first commit. Returns (row, row lock) or (None, None)."""
    stmt = select(PlsSearchHarvestQuery).where(
        PlsSearchHarvestQuery.source_name == SOURCE_NAME,
        PlsSearchHarvestQuery.status.in_(("pending", "in_progress")),
    )
    if not take_over:
        stmt = stmt.where(_share_filter(slot))
    rows = (await db.execute(_pick_order(stmt).limit(candidates).with_for_update(skip_locked=True))).scalars().all()
    for row in rows:
        lock = _query_lock(row.id, redis_client)
        try:
            await lock.acquire()
        except SessionLockHeld:
            await lock.release()
            continue
        row.status = "in_progress"
        row.slot = slot
        # stamped at once, so a row that ends its run early (an interrupted page) goes behind its equals
        row.last_run_at = datetime.now(timezone.utc)
        await db.commit()
        return row, lock
    await db.commit()  # ends the transaction that held the candidates' row locks
    return None, None


def _slot_known_good_at(slot_row: BrowserSessionSlot) -> Optional[datetime]:
    seen = [t for t in (slot_row.last_used_at, slot_row.last_verified_at, slot_row.logged_in_at) if t is not None]
    return max(seen) if seen else slot_row.updated_at


async def slot_takeover_due(db: AsyncSession, slot: int, *, now: Optional[datetime] = None) -> bool:
    """True when the other login has been logged out (not ACTIVE) for PLS_SEARCH_HARVEST_TAKEOVER_MINUTES, counted
    from the last time it was known to work, so `slot` may also work the other slot's share."""
    other = 2 if slot == 1 else 1
    row = (
        await db.execute(
            select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == SOURCE_NAME, BrowserSessionSlot.slot_number == other)
        )
    ).scalars().first()
    if row is None:
        return True  # there is no other login at all: its share is nobody's but this one's
    if row.state == "ACTIVE":
        return False
    since = _slot_known_good_at(row)
    if since is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now - since >= timedelta(minutes=int(settings.PLS_SEARCH_HARVEST_TAKEOVER_MINUTES))


class SearchHarvestRunner:
    def __init__(
        self,
        db: AsyncSession,
        source: ScraperSource,
        *,
        redis_client=None,
        job_id=None,
        dry_run: bool = False,
    ):
        self.db = db
        self.source = source
        self.pipeline = PakistanLawSitePipeline(db, source, redis_client=redis_client, job_id=job_id)
        self.dry_run = dry_run
        self.memory = PlsBrowserMemoryState()
        self.harvest_mode = "updates"
        self.pacing_profile = login_pacing_profile("updates")
        # Verification is Account-2-only. Acquisition never substitutes its own session for the verifier.
        self.spot_check_between_pages = False

    async def _pace_sleep(self) -> None:
        lo = float(self.pacing_profile["login_delay_min"])
        hi = float(self.pacing_profile["login_delay_max"])
        await self.pipeline.sleep(random.uniform(lo, hi))

    async def _maybe_recycle_browser(self) -> None:
        should_recycle, stop_reason = check_rss_limits(self.memory)
        if stop_reason:
            raise PlsBrowserMemoryHardLimit(stop_reason)
        if should_recycle:
            try:
                await self.pipeline.runner.close()
            except Exception:
                pass
            self.pipeline.runner.browser = None
            self.memory.reset_recycle_counter()
            logger.info("PLS search harvest recycled browser after memory/window guard")

    async def _maybe_spot_check(self) -> None:
        """Verification is a separate Account-2 task; harvester pages never claim its verification turn."""
        return


    async def run_query(
        self,
        query_row: PlsSearchHarvestQuery,
        search_map: Dict[str, Any],
        *,
        max_pages: int = 0,
    ) -> Dict[str, Any]:
        query_json = dict(query_row.query_json or {})
        cursor = dict(query_row.cursor_json or {})
        if not cursor:
            cursor = {"page": 1, "row_index": 0}
        reason = unmapped_harvest_reason(search_map, query_json, cursor)
        if reason:
            query_row.status = "failed"
            query_row.last_error = reason[:1000]
            return {"status": "failed", "reason": reason}
        cap = int(getattr(settings, "PLS_SEARCH_RESULT_CAP", 500) or 500)
        pages_done = int(query_row.pages_enumerated or 0)
        page_limit = max_pages if max_pages > 0 else int(getattr(settings, "PLS_SEARCH_HARVEST_MAX_PAGES_PER_QUERY", 0) or 0)
        rows_seen = int(query_row.rows_seen or 0)
        rows_known = int(query_row.rows_known or 0)
        rows_new = int(query_row.rows_new or 0)
        site_total = query_row.site_total_results
        start_row_index = int(cursor.get("row_index") or 0)
        page_idx = int(cursor.get("page") or 1)

        budget = int(getattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0) or 0)
        deadline = time.monotonic() + budget if budget > 0 else None
        out_of_time = False

        # Judgments already met on an earlier page of this run: the site has served page 1's rows again as page 2,
        # and a repeated row is neither opened nor counted a second time.
        seen_keys: Set[str] = set()
        repeat_pages = 0

        resume_url = cursor.get("next_url") if pages_done > 0 else None
        # The run opens on a page an earlier run already finished: a page that showed no next link and no site
        # total is read once more to confirm it is the last. Its rows were counted then and are not counted again,
        # so the confirming read can neither inflate rows_seen into a false cap split nor loop for ever
        # (12 October 2026: SCMR, CLC and PCrLJ 2017 re-read page 1 every tick).
        rereading = not resume_url and pages_done > 0 and pages_done >= page_idx
        if resume_url:
            page = await self.pipeline.fetch_detail(str(resume_url))
        else:
            values = build_harvest_form_values(search_map, query_json, cursor)
            page = await self.pipeline.fetch_results(search_map, values)
        self.memory.note_window()
        await self._maybe_recycle_browser()

        while True:
            from scraper.extractors.hybrid_extractor import HybridExtractor

            extractor = HybridExtractor(self.db, self.source, local=self.pipeline.local_engine)
            outcome = await extractor.extract_result_rows(html=page.html, search_map=search_map, base_url=page.url)
            rows = outcome.data.get("result_rows") or []
            repeated = 0
            fresh_rows: List[Dict[str, Any]] = []
            for row in rows:
                keys = citation_keys_for_row(row)
                if keys and keys & seen_keys:
                    repeated += 1
                    continue
                seen_keys.update(keys)
                fresh_rows.append(row)
            fresh_ids = {id(row) for row in fresh_rows}
            if repeated:
                logger.info("PLS search harvest %s page %s: %s row(s) repeat an earlier page and are skipped", query_row.query_key, page_idx, repeated)
            # Every row of a non-empty page already met: the site is serving an earlier page again.
            repeat_pages = repeat_pages + 1 if rows and not fresh_rows else 0
            if site_total is None:
                site_total = outcome.data.get("total_results_if_shown") or parse_total_results_from_html(page.html)
            lookup_keys: Set[str] = set()
            for row in fresh_rows:
                lookup_keys.update(citation_keys_for_row(row))
            known_set = await load_known_citation_keys(self.db, lookup_keys)
            known_on_page, new_rows = partition_rows_by_known(fresh_rows, known_set)
            if rereading:
                rows_counted, known_counted = 0, 0
            else:
                rows_counted, known_counted = len(fresh_rows), known_on_page
            rows_seen += rows_counted
            rows_known += known_counted

            if not self.dry_run:
                route_base = {
                    "harvest": "search_gap",
                    "query_key": query_row.query_key,
                    "query": query_json,
                }
                for idx, row in enumerate(rows):
                    if idx < start_row_index:
                        continue
                    detail_url = row.get("detail_url") or row.get("pdf_url")
                    if not detail_url:
                        continue
                    keys = citation_keys_for_row(row)
                    if keys & known_set:
                        continue
                    if id(row) not in fresh_ids:
                        continue
                    detail = await self.pipeline.fetch_detail(detail_url)
                    result = await self.pipeline.preserve_and_extract(
                        detail,
                        {**route_base, "cursor": dict(cursor), "row_index": idx},
                        row,
                    )
                    if result == "staged":
                        rows_new += 1
                    release_page_result(detail)
                    cursor["row_index"] = idx + 1
                    # A new dict every time: the same dict changed in place and assigned back is not seen as a change
                    # after the first flush, so the stored row_index stayed at 1-9 while ticks reached row 67+. Commit
                    # per row so a killed or redeployed tick keeps its place and its staged rows.
                    query_row.cursor_json = dict(cursor)
                    query_row.rows_new = rows_new
                    await self.db.commit()
                    await self._pace_sleep()
                    await self._maybe_recycle_browser()
                    if deadline is not None and time.monotonic() >= deadline and idx + 1 < len(rows):
                        out_of_time = True
                        break
            else:
                rows_new += len(new_rows)

            if out_of_time:
                # Stop mid-page and give the login worker back; the cursor (this page, next row) resumes the
                # query on the next tick, which counts this page's rows again, so they are not counted now.
                rows_seen -= rows_counted
                rows_known -= known_counted
                release_page_result(page)
                query_row.rows_seen = rows_seen
                query_row.rows_known = rows_known
                query_row.rows_new = rows_new
                query_row.site_total_results = site_total
                query_row.last_run_at = datetime.now(timezone.utc)
                query_row.status = "in_progress"
                await self.db.flush()
                break

            confirmed_last = rereading and not outcome.data.get("next_page")
            if not rereading:
                pages_done += 1
            rereading = False
            start_row_index = 0
            cursor["row_index"] = 0
            nxt = outcome.data.get("next_page")
            release_page_result(page)
            try:
                browser = self.pipeline.runner.browser
                if browser is not None:
                    await browser.release_citation_grid_dom()
            except Exception:
                pass

            gap = compute_gap_size(site_total, rows_known, rows_seen)
            query_row.site_total_results = site_total
            query_row.pages_enumerated = pages_done
            query_row.rows_seen = rows_seen
            query_row.rows_known = rows_known
            query_row.rows_new = rows_new
            query_row.gap_size = gap
            if site_total is not None:
                # Only a known site total measures the gap. With the total unknown the gap reads 0, and 1000 - 0
                # sent the row to the back of the ledger, behind every row that had one (13 October 2026); it keeps
                # its planned priority instead.
                query_row.priority = max(1, 1000 - gap)
            query_row.last_run_at = datetime.now(timezone.utc)
            await self.db.flush()

            if page_limit and pages_done >= page_limit:
                cursor["page"] = page_idx
                query_row.cursor_json = dict(cursor)
                query_row.status = "in_progress"
                break
            if not nxt:
                if is_capped(site_total, rows_seen, cap):
                    # A list that reaches the cap is truncated by the site: never complete. Narrow it, or fail it
                    # loudly when nothing narrower can be expressed.
                    children = split_oversized_query(
                        query_json,
                        int(site_total if site_total is not None else rows_seen),
                        result_cap=cap,
                        court_options=_court_options_from_map(search_map),
                        available_roles=set((search_map.get("fields") or {}).keys()) | {"court", "keyword"},
                        bench_options=[str(o) for o in ((search_map.get("fields") or {}).get("bench") or {}).get("options") or []],
                    )
                    if children:
                        await self._enqueue_split_children(query_row, children, int(site_total or rows_seen), cap)
                        query_row.status = "split"
                        query_row.split_reason = f"site_total={site_total} rows_seen={rows_seen} reached cap={cap}"
                        break
                    query_row.status = "failed"
                    query_row.last_error = f"capped at {cap} results and no narrower dimension left to split on (exhausted); NOT complete"
                    break
                may_done, incomplete_reason = search_harvest_may_mark_done(
                    site_total=site_total,
                    rows_seen=rows_seen,
                    rows_on_page=len(rows),
                )
                if not may_done and confirmed_last and site_total is None:
                    may_done = True
                if may_done:
                    query_row.status = "done"
                    query_row.last_error = None if confirmed_last else query_row.last_error
                    query_row.cursor_json = {"page": 1, "row_index": 0}
                else:
                    query_row.status = "in_progress"
                    query_row.last_error = incomplete_reason[:1000]
                    if site_total is not None and rows_seen < int(site_total):
                        cursor = {"page": page_idx + 1, "row_index": 0}
                    else:
                        cursor = {"page": page_idx, "row_index": 0}
                    query_row.cursor_json = dict(cursor)
                break
            page_idx += 1
            cursor = {"page": page_idx, "row_index": 0, "next_url": nxt}
            query_row.cursor_json = dict(cursor)
            if repeat_pages >= 2:
                # Two pages in a row brought nothing new: the next link leads back over pages already read. Stop
                # here, keep the place, and say why, instead of walking the same rows until the time runs out.
                query_row.status = "in_progress"
                query_row.last_error = f"pages {page_idx - 2} and {page_idx - 1} repeated rows of earlier pages; pagination stopped for this run"[:1000]
                break
            if deadline is not None and time.monotonic() >= deadline:
                out_of_time = True
                query_row.status = "in_progress"
                break
            await self.db.commit()
            await self._maybe_spot_check()
            await self._pace_sleep()
            try:
                page = await self.pipeline.fetch_detail(nxt)
            except Exception as exc:
                query_row.status = "in_progress"
                query_row.last_error = f"pagination interrupted: {exc}"[:1000]
                break
            self.memory.note_window()
            await self._maybe_recycle_browser()

        return {
            "query_key": query_row.query_key,
            "status": query_row.status,
            "site_total_results": site_total,
            "pages_enumerated": pages_done,
            "rows_seen": rows_seen,
            "rows_known": rows_known,
            "rows_new": rows_new,
            "gap_size": query_row.gap_size,
            "dry_run": self.dry_run,
            "out_of_time": out_of_time,
        }

    async def _enqueue_split_children(
        self,
        parent: PlsSearchHarvestQuery,
        children: List[Dict[str, Any]],
        site_total: int,
        cap: int,
    ) -> None:
        existing = {
            r.query_key
            for r in (await self.db.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME))).scalars().all()
        }
        for child_json in children:
            key = make_query_key(child_json)
            if key in existing:
                continue
            self.db.add(
                PlsSearchHarvestQuery(
                    source_name=SOURCE_NAME,
                    query_key=key,
                    query_json=child_json,
                    parent_id=parent.id,
                    status="pending",
                    priority=500 + int(site_total / max(cap, 1)),
                    split_reason=f"parent {parent.query_key} site_total={site_total}",
                )
            )
            existing.add(key)


async def _slot_state(db: AsyncSession, slot: int) -> Optional[str]:
    row = (
        await db.execute(
            select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == SOURCE_NAME, BrowserSessionSlot.slot_number == slot)
        )
    ).scalars().first()
    return row.state if row is not None else None


async def run_search_harvest(
    db: AsyncSession,
    *,
    slot: Optional[int] = None,
    query_key: Optional[str] = None,
    dry_run: bool = False,
    max_pages: int = 0,
    priority_gaps: bool = False,
    redis_client=None,
    job_id=None,
) -> Dict[str, Any]:
    """One search-harvest tick on Account 1.

    Owner ruling 16 October 2026: slot 1 is acquisition; slot 2 is an independent verifier.
    No takeover or manual fallback may turn the verifier account into an acquisition account.
    """
    if not settings.login_scraping_effective:
        return {"skipped": True, "reason": "login scraping not permitted in this environment"}
    source = await _source_row(db)
    if source.state in ("HALTED", "DISABLED", "PAUSED"):
        return {"skipped": True, "reason": f"source state {source.state}"}
    harvest_slot = 1
    if slot is not None and int(slot) != harvest_slot:
        return {"skipped": True, "reason": f"slot {slot} is reserved for independent verification", "slot": int(slot)}
    slots = [harvest_slot]

    harvest_lock: Optional[SessionLock] = None
    chosen: Optional[int] = None
    reasons: List[str] = []
    for n in slots:
        state = await _slot_state(db, n)
        if state != "ACTIVE":
            reasons.append(f"harvester slot {n} is {state or 'missing'}; verifier slot is never used for acquisition")
            continue
        # one harvest per slot (R3.7): a second tick for the same slot exits at once
        candidate = SessionLock(f"{SOURCE_NAME}:search_harvest:slot{n}", redis_client, max_holders=1)
        try:
            await candidate.acquire()
        except SessionLockHeld:
            await candidate.release()
            reasons.append(f"a search harvest is already running on slot {n}")
            continue
        harvest_lock, chosen = candidate, n
        break
    if harvest_lock is None or chosen is None:
        await db.commit()
        return {"skipped": True, "slot": slot, "reason": "; ".join(reasons) or "no login slot"}

    query_lock: Optional[SessionLock] = None
    lock: Optional[SessionLock] = None
    slot_lock: Optional[SessionLock] = None
    runner: Optional[SearchHarvestRunner] = None
    query_row: Optional[PlsSearchHarvestQuery] = None
    took_over = False
    try:
        if query_key:
            query_row = await pick_next_query(db, query_key=query_key)
            if query_row is not None:
                query_lock = _query_lock(query_row.id, redis_client)
                try:
                    await query_lock.acquire()
                except SessionLockHeld:
                    return {"skipped": True, "slot": chosen, "reason": f"query {query_key} is being run by another tick"}
                query_row.status, query_row.slot = "in_progress", chosen
                await db.commit()
        else:
            query_row, query_lock = await claim_next_query(db, chosen, redis_client=redis_client)
        if query_row is None:
            return {"skipped": True, "slot": chosen, "reason": f"no pending search harvest query in slot {chosen}'s share"}
        m = await active_map(db, SOURCE_NAME)
        if m is None:
            query_row.status = "pending"
            query_row.last_error = "no active search form map"
            await db.commit()
            return {"skipped": True, "slot": chosen, "reason": "no active search form map"}
        search_map = map_as_dict(m)
        dashboard_harvest = PakistanLawSitePipeline._is_dashboard_citation_fields(search_map)

        runner = SearchHarvestRunner(db, source, redis_client=redis_client, job_id=job_id, dry_run=dry_run)
        runner.harvest_mode = await get_harvest_mode(db)
        runner.pacing_profile = login_pacing_profile(runner.harvest_mode)
        runner.pipeline.harvest_mode = runner.harvest_mode
        runner.pipeline.pacing_profile = runner.pacing_profile

        active_slot_count = len([s for s in await runner.pipeline.manager.slots() if s.state == "ACTIVE"])
        max_holders = max(1, min(int(runner.pacing_profile.get("login_session_concurrency") or 1), active_slot_count))
        lock = SessionLock(SOURCE_NAME, redis_client, max_holders=max_holders)
        try:
            await lock.acquire()
        except SessionLockHeld:
            query_row.status = "pending"
            await db.commit()
            return {"skipped": True, "slot": chosen, "reason": "login session lock held"}
        # This tick's own login only, never the other (two browsers on one login end each other, R3.5).
        slot_lock = SessionLock(f"{SOURCE_NAME}:slot{chosen}", redis_client, max_holders=1)
        try:
            await slot_lock.acquire()
        except SessionLockHeld:
            query_row.status = "pending"
            await db.commit()
            return {"skipped": True, "slot": chosen, "reason": f"login slot {chosen} is in use by another job"}
        runner.pipeline._session_lock = lock
        runner.pipeline._slot_lock = slot_lock
        runner.pipeline.runner.preferred_slot_number = chosen
        runner.pipeline.runner.exclusive_slot = True
        runner.pipeline._run_locks.extend([harvest_lock, query_lock])
        runner.pipeline._assert_permitted()
        if not dashboard_harvest:
            await runner.pipeline.ensure_search_map()
            m = await active_map(db, SOURCE_NAME)
            search_map = map_as_dict(m) if m else search_map
        result = await runner.run_query(query_row, search_map, max_pages=max_pages)
        await db.commit()
        return {**result, "slot": chosen, "took_over": took_over}
    except (PacingBudgetExceeded, PlsBrowserMemoryHardLimit, LoginRequired) as exc:
        if query_row is not None:
            query_row.status = "pending"
            query_row.last_error = str(exc)[:1000]
        await db.commit()
        return {"paused": True, "slot": chosen, "reason": str(exc), "query_key": query_row.query_key if query_row else None}
    finally:
        if runner is not None:
            try:
                await runner.pipeline.runner.close()
            except Exception:
                pass
            runner.pipeline._slot_lock = None
        for held in (slot_lock, lock, query_lock, harvest_lock):
            if held is not None:
                await held.release()


def _continue_slot(result: Dict[str, Any]) -> Optional[int]:
    """The slot whose next tick is queued at once: a tick that worked a row and was not paused (a spent page
    budget, a lost login, the memory guard) or skipped. Anything else waits for the beat."""
    if not settings.PLS_SEARCH_HARVEST_CONTINUE:
        return None
    if result.get("skipped") or result.get("paused") or not result.get("query_key"):
        return None
    slot = result.get("slot")
    return 1 if slot == 1 else None


def queue_search_harvest_ticks(app, *, slots=(1,)) -> List[int]:
    """Queue acquisition only on Account 1. Account 2 is reserved for independent verification."""
    queued: List[int] = []
    for n in slots:
        if int(n) != 1:
            continue
        app.send_task("scraper.tasks.pls_search_harvest.pls_search_harvest_tick", kwargs={"slot": 1}, queue="login_session")
        queued.append(1)
    return queued


@shared_task(name="scraper.tasks.pls_search_harvest.pls_search_harvest_tick")
def pls_search_harvest_tick(**kwargs) -> Dict[str, Any]:
    from scraper.tasks.deploy_hold import login_work_held

    if login_work_held():
        return {"skipped": True, "reason": "deploy_hold"}

    async def _inner() -> Dict[str, Any]:
        from sqlalchemy import func, select

        from scraper.pls_grid_health import (
            SOURCE_NAME,
            pls_last_judgment_at,
            pls_search_harvest_may_run,
            pls_stall_verdict_for_config,
        )
        from scraper.models import PlsSearchHarvestQuery, ScraperSource

        async with SessionLocal() as db:
            source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
            cfg = dict(source.config_json or {}) if source else {}
            pending = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(PlsSearchHarvestQuery)
                        .where(
                            PlsSearchHarvestQuery.source_name == SOURCE_NAME,
                            PlsSearchHarvestQuery.status.in_(("pending", "in_progress")),
                        )
                    )
                ).scalar()
                or 0
            )
            stall_reason = None
            if pending > 0 and source is not None:
                last_j = await pls_last_judgment_at(db)
                stall_reason = pls_stall_verdict_for_config(
                    cfg, last_promotion_at=last_j, source_state=source.state
                ).get("stalled_reason")
            if not pls_search_harvest_may_run(cfg, pending_gaps=pending, stall_reason=stall_reason):
                return {"skipped": True, "reason": "PLS_SEARCH_HARVEST_ENABLED is not set and harvest is not stalled"}
            return await run_search_harvest(db, **kwargs)

    result = run_async(_inner())
    next_slot = _continue_slot(result)
    if next_slot is not None:
        from scraper.tasks.celery_app import app

        queue_search_harvest_ticks(app, slots=(next_slot,))
        result["queued_next_tick"] = next_slot
    return result


def cli_plan(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    async def _run():
        async with SessionLocal() as db:
            return await seed_search_harvest_plan(db, dry_run=args.dry_run)

    print(json.dumps(run_async(_run()), indent=2))
    return 0


def cli_gap_report(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)

    async def _run():
        async with SessionLocal() as db:
            return await gap_report_rows(db, limit=args.limit)

    print(json.dumps(run_async(_run()), indent=2))
    return 0


def cli_run(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--query-key", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--priority-gaps", action="store_true")
    args = parser.parse_args(argv)

    async def _run():
        async with SessionLocal() as db:
            return await run_search_harvest(
                db,
                query_key=args.query_key or None,
                dry_run=args.dry_run,
                max_pages=args.max_pages,
                priority_gaps=args.priority_gaps,
            )

    print(json.dumps(run_async(_run()), indent=2))
    return 0


def cli_extended_plan(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--party", action="append", default=[])
    args = parser.parse_args(argv)

    async def _run():
        async with SessionLocal() as db:
            out = await seed_extended_plan(db, dry_run=args.dry_run, keywords=args.keyword, parties=args.party)
            await db.commit()
            return out

    print(json.dumps(_run_sync(_run()), indent=2))
    return 0


def _run_sync(coro):
    return run_async(coro)


def cli_snowball(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=2000)
    args = parser.parse_args(argv)

    async def _run():
        async with SessionLocal() as db:
            out = await seed_snowball(db, limit=args.limit, dry_run=args.dry_run)
            await db.commit()
            return out

    print(json.dumps(run_async(_run()), indent=2))
    return 0


def cli_completeness(argv: Optional[List[str]] = None) -> int:
    async def _run():
        async with SessionLocal() as db:
            return await completeness_report(db)

    print(json.dumps(run_async(_run()), indent=2, default=str))
    return 0


def cli_reopen_premature_done(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    async def _run():
        async with SessionLocal() as db:
            out = await reopen_premature_done_queries(db, dry_run=args.dry_run)
            if not args.dry_run:
                await db.commit()
            return out

    print(json.dumps(run_async(_run()), indent=2))
    return 0
