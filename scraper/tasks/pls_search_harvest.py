"""
PakistanLawSite search-driven gap harvester: systematic search-bar queries, full result pagination,
gap accounting, and resumable progress in pls_search_harvest_query.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from celery import shared_task
from sqlalchemy import select
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
    Citation,
    Judgment,
    PlsSearchHarvestQuery,
    ScraperSource,
    ScraperStaging,
)
from scraper.pls_browser_memory import PlsBrowserMemoryHardLimit, PlsBrowserMemoryState, check_rss_limits
from scraper.pls_search_harvest_core import (
    build_harvest_form_values,
    citation_keys_for_row,
    compute_gap_size,
    gap_report_sort_key,
    iter_base_plan_queries,
    make_query_key,
    normalize_query_json,
    parse_total_results_from_html,
    partition_rows_by_known,
    reporters_for_plan,
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
    key_list = list(keys)
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

    from scraper.models import Citation, Judgment

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
    return {
        "queue": status_counts,
        "done_but_capped": capped_open,
        "capped_exhausted_not_complete": exhausted,
        "site_total_done_queries": int(totals[0]),
        "rows_known_done_queries": int(totals[1]),
        "page_continuity_holes": {"reporter_years_with_holes": len(holes), "examples": dict(list(holes.items())[:20])},
        "known_citation_test": {"sampled": len(sample), "found": len(known & set(sample))},
        "complete": bool(status_counts.get("pending", 0) + status_counts.get("in_progress", 0) + status_counts.get("failed", 0) + capped_open == 0 and not holes),
    }


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


async def pick_next_query(
    db: AsyncSession,
    *,
    query_key: Optional[str] = None,
    priority_gaps: bool = False,
) -> Optional[PlsSearchHarvestQuery]:
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
    if priority_gaps:
        stmt = stmt.order_by(PlsSearchHarvestQuery.gap_size.desc(), PlsSearchHarvestQuery.priority.asc())
    else:
        stmt = stmt.order_by(PlsSearchHarvestQuery.priority.asc(), PlsSearchHarvestQuery.created_at.asc())
    return (await db.execute(stmt.limit(1))).scalars().first()


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

        resume_url = cursor.get("next_url") if pages_done > 0 else None
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
            if site_total is None:
                site_total = outcome.data.get("total_results_if_shown") or parse_total_results_from_html(page.html)
            lookup_keys: Set[str] = set()
            for row in rows:
                lookup_keys.update(citation_keys_for_row(row))
            known_set = await load_known_citation_keys(self.db, lookup_keys)
            known_on_page, new_rows = partition_rows_by_known(rows, known_set)
            rows_seen += len(rows)
            rows_known += known_on_page

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
                    query_row.cursor_json = cursor
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
                rows_seen -= len(rows)
                rows_known -= known_on_page
                release_page_result(page)
                query_row.rows_seen = rows_seen
                query_row.rows_known = rows_known
                query_row.rows_new = rows_new
                query_row.site_total_results = site_total
                query_row.last_run_at = datetime.now(timezone.utc)
                query_row.status = "in_progress"
                await self.db.flush()
                break

            pages_done += 1
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
            query_row.priority = max(1, 1000 - gap)
            query_row.last_run_at = datetime.now(timezone.utc)
            await self.db.flush()

            if page_limit and pages_done >= page_limit:
                cursor["page"] = page_idx
                query_row.cursor_json = cursor
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
                query_row.status = "done"
                query_row.cursor_json = {"page": 1, "row_index": 0}
                break
            page_idx += 1
            cursor = {"page": page_idx, "row_index": 0, "next_url": nxt}
            query_row.cursor_json = cursor
            if deadline is not None and time.monotonic() >= deadline:
                out_of_time = True
                query_row.status = "in_progress"
                break
            await self._pace_sleep()
            page = await self.pipeline.fetch_detail(nxt)
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


async def run_search_harvest(
    db: AsyncSession,
    *,
    query_key: Optional[str] = None,
    dry_run: bool = False,
    max_pages: int = 0,
    priority_gaps: bool = False,
    redis_client=None,
    job_id=None,
) -> Dict[str, Any]:
    if not settings.login_scraping_effective:
        return {"skipped": True, "reason": "login scraping not permitted in this environment"}
    source = await _source_row(db)
    if source.state in ("HALTED", "DISABLED", "PAUSED"):
        return {"skipped": True, "reason": f"source state {source.state}"}
    query_row = await pick_next_query(db, query_key=query_key, priority_gaps=priority_gaps)
    if query_row is None:
        return {"skipped": True, "reason": "no pending search harvest query"}
    m = await active_map(db, SOURCE_NAME)
    if m is None:
        query_row.last_error = "no active search form map"
        return {"skipped": True, "reason": "no active search form map"}
    search_map = map_as_dict(m)
    dashboard_harvest = PakistanLawSitePipeline._is_dashboard_citation_fields(search_map)
    query_row.status = "in_progress"
    await db.flush()

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
        return {"skipped": True, "reason": "login session lock held"}
    try:
        runner.pipeline._session_lock = lock
        runner.pipeline._assert_permitted()
        if not dashboard_harvest:
            await runner.pipeline.ensure_search_map()
            m = await active_map(db, SOURCE_NAME)
            search_map = map_as_dict(m) if m else search_map
        result = await runner.run_query(query_row, search_map, max_pages=max_pages)
        await db.commit()
        return result
    except (PacingBudgetExceeded, PlsBrowserMemoryHardLimit) as exc:
        query_row.status = "pending"
        query_row.last_error = str(exc)[:1000]
        await db.commit()
        return {"paused": True, "reason": str(exc), "query_key": query_row.query_key}
    except LoginRequired as exc:
        query_row.status = "pending"
        query_row.last_error = str(exc)[:1000]
        await db.commit()
        return {"paused": True, "reason": str(exc), "query_key": query_row.query_key}
    finally:
        try:
            await runner.pipeline.runner.close()
        except Exception:
            pass
        await lock.release()


@shared_task(name="scraper.tasks.pls_search_harvest.pls_search_harvest_tick")
def pls_search_harvest_tick(**kwargs) -> Dict[str, Any]:
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

    return run_async(_inner())


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
