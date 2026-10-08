"""PakistanLawSite case-ID walk (Beat task): ask the site for each judgment by its own serial.

scraper/pls_caseid.py explains the numbering and plans the walk. Each tick holds the login-session
lock, takes up to PLS_CASEID_WALK_PROBES_PER_TICK serials at the login pacing, sends every page
that is a judgment through the ordinary raw-first pipeline (provenance, staging, extraction,
promotion, archive copy) and records progress in the source's config_json under `caseid_walk`.

Before the first walk (and again after a failed check, every PLS_CASEID_WALK_RECHECK_HOURS) it
fetches two judgments the corpus already holds and requires both to be recognised as judgment
pages; otherwise it stops and says why instead of spending the page budget on pages it cannot read.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from celery import shared_task
from sqlalchemy import or_, select

from scraper.auth.session_manager import LoginRequired, SessionLock, SessionLockHeld, merge_source_config, release_page_result
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.extractors.judgment_guards import strip_leading_judgment_chrome
from scraper.harvest_mode import get_harvest_mode, login_pacing_profile
from scraper.models import Judgment, ScraperSource, ScraperStaging
from scraper.parsers.text_cleaner import clean_html
from scraper.pls_caseid import case_name, case_page_diagnostics, case_url, ceiling, group_order, known_by_group, looks_like_case_page, new_state, next_serial, record, same_judgment, split_group_key
from scraper.tasks.pakistanlawsite import PacingBudgetExceeded, PakistanLawSitePipeline, SOURCE_NAME

logger = logging.getLogger(__name__)

STATE_KEY = "caseid_walk"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def page_text(page) -> str:
    modal = strip_leading_judgment_chrome((page.metadata or {}).get("case_description_modal_text") or "")
    return modal or clean_html(page.html or "")


async def load_known(db) -> Dict[str, Set[int]]:
    """Serials already in the corpus or in staging (any status), so none is asked for twice."""
    urls: List[Optional[str]] = []
    for model in (Judgment, ScraperStaging):
        rows = await db.execute(select(model.source_url).where(model.source_name == SOURCE_NAME, model.source_url.like("%CaseName=%")))
        urls.extend(r[0] for r in rows.all())
    return known_by_group(urls)


def _calibration_due(state: Dict[str, Any], now: datetime) -> bool:
    if state.get("calibrated_at"):
        return False
    failed = state.get("calibration_failed_at")
    if not failed:
        return True
    if any("matched_by" not in r for r in state.get("calibration") or []):
        return True  # failed under the old citation-only check: try again at once with the held text
    try:
        at = datetime.fromisoformat(str(failed))
    except ValueError:
        return True
    return now - at >= timedelta(hours=float(settings.PLS_CASEID_WALK_RECHECK_HOURS))


class CaseIdWalker:
    def __init__(self, db, source: ScraperSource, *, redis_client=None, job_id=None, **pipeline_kwargs):
        self.db = db
        self.source = source
        self.pipeline = PakistanLawSitePipeline(db, source, redis_client=redis_client, job_id=job_id, **pipeline_kwargs)
        self.pacing_profile = login_pacing_profile("updates")
        self.stats: Dict[str, Any] = {"probes": 0, "hits": 0, "misses": 0, "staged": 0, "duplicates": 0, "groups": []}

    async def _pace(self) -> None:
        lo, hi = float(self.pacing_profile["login_delay_min"]), float(self.pacing_profile["login_delay_max"])
        await self.pipeline.sleep(random.uniform(lo, hi))

    async def _save(self, state: Dict[str, Any]) -> None:
        await merge_source_config(self.db, self.source, {STATE_KEY: state})
        await self.db.commit()

    async def _held_text(self, key: str, serial: int) -> str:
        # Match parse_case_id: serial must not be a prefix of a longer serial (1 vs 10); the case
        # name ends at "&" or at the end of the link.
        name = case_name(key, serial)

        def url_match(col):
            return or_(col.like(f"%CaseName={name}&%"), col.like(f"%CaseName={name}"))

        row = (await self.db.execute(select(Judgment.full_text).where(Judgment.source_name == SOURCE_NAME, url_match(Judgment.source_url)).limit(1))).first()
        if row and row[0]:
            return row[0]
        row = (await self.db.execute(select(ScraperStaging.raw_text).where(ScraperStaging.source_name == SOURCE_NAME, url_match(ScraperStaging.source_url)).limit(1))).first()
        return (row[0] if row else None) or ""

    async def calibrate(self, state: Dict[str, Any], known: Dict[str, Set[int]]) -> bool:
        """Fetch two held judgments; each must come back as the judgment we hold (its own text), or,
        where we hold no text for it, read as a judgment page."""
        densest = max(known, key=lambda k: len(known[k]))
        year, _ = split_group_key(densest)
        samples = sorted(known[densest])[:2]
        results = []
        for serial in samples:
            page = await self.pipeline.fetch_detail(case_url(settings.PLS_BASE_URL, densest, serial))
            text = page_text(page)
            held = await self._held_text(densest, serial)
            if held:
                ok, matched = same_judgment(text, held), "held_text"
            else:
                ok, matched = looks_like_case_page(text, year, raw_html=page.html), "page_check"
            results.append({"case": f"{densest}{serial}", "ok": ok, "matched_by": matched, **case_page_diagnostics(text, raw_html=page.html)})
            release_page_result(page)
            await self._pace()
        if all(r["ok"] for r in results):
            state.update({"calibrated_at": _now().isoformat(), "calibration": results})
            state.pop("calibration_failed_at", None)
            logger.info("PakistanLawSite case-ID walk calibrated on held judgments: %s", results)
            return True
        state.update({"calibration_failed_at": _now().isoformat(), "calibration": results})
        logger.warning("PakistanLawSite case-ID walk NOT started: held judgments did not read as judgment pages: %s", results)
        return False

    async def run(self, probes: int) -> Dict[str, Any]:
        now = _now()
        state: Dict[str, Any] = dict((self.source.config_json or {}).get(STATE_KEY) or {})
        groups: Dict[str, Dict[str, Any]] = dict(state.get("groups") or {})
        known = await load_known(self.db)
        if not known:
            return {"skipped": "no case IDs in the corpus to learn the numbering from"}
        if _calibration_due(state, now):
            ok = await self.calibrate(state, known)
            await self._save(state)
            if not ok:
                return {"stopped": "calibration failed", "calibration": state.get("calibration")}
        elif not state.get("calibrated_at"):
            return {"skipped": "calibration failed recently; waiting for the recheck", "calibration": state.get("calibration")}

        years = [split_group_key(k)[0] for k in known]
        first_year = int(settings.PLS_EARLIEST_YEAR or 0) or min(years)
        order = group_order(known, first_year=min(first_year, min(years)), last_year=now.year)
        floor, pad, streak = int(settings.PLS_CASEID_WALK_CEILING), int(settings.PLS_CASEID_WALK_CEILING_PAD), int(settings.PLS_CASEID_WALK_MISS_STREAK)
        miss_sampled = False
        for key in order:
            if self.stats["probes"] >= probes:
                break
            g = groups.get(key) or new_state()
            year, _ = split_group_key(key)
            held = known.setdefault(key, set())
            if g.get("done"):
                probe = max(1, int(g.get("next") or 1))
                while probe in held:
                    probe += 1
                if probe <= ceiling(g, held, floor=floor, pad=pad):
                    g["done"] = False
                else:
                    continue
            state["current"] = key
            first_serial = None
            while self.stats["probes"] < probes:
                serial = next_serial(g, held, floor=floor, pad=pad)
                if serial is None:
                    logger.info("PakistanLawSite case-ID walk: group %s done (hits %s, misses %s)", key, g.get("hits"), g.get("misses"))
                    break
                first_serial = first_serial or serial
                url = case_url(settings.PLS_BASE_URL, key, serial)
                page = await self.pipeline.fetch_detail(url)
                self.stats["probes"] += 1
                text = page_text(page)
                hit = looks_like_case_page(text, year, raw_html=page.html)
                if hit:
                    route = {"harvest": "caseid_walk", "case_id": f"{key}{serial}"}
                    outcome = await self.pipeline.preserve_and_extract(page, route, {"citation": None, "title": None, "court": None, "detail_url": url, "pdf_url": None})
                    self.stats["hits"] += 1
                    self.stats["staged" if outcome == "staged" else "duplicates"] += 1
                else:
                    self.stats["misses"] += 1
                    if not miss_sampled:
                        miss_sampled = True
                        from scraper.pls_navigation import classify_pls_page

                        logger.info(
                            "PakistanLawSite case-ID walk: %s%s is not a judgment page (page type %s, %s chars of text)",
                            key, serial, classify_pls_page(page.html or "", page.url or "", page.metadata), len(text),
                        )
                release_page_result(page)
                record(g, serial, hit, held, miss_streak=streak)
                groups[key] = g
                state["groups"] = groups
                state["last_probe_at"] = _now().isoformat()
                await self._save(state)
            groups[key] = g
            state["groups"] = groups
            await self._save(state)
            self.stats["groups"].append({"group": key, "from": first_serial, "next": g.get("next"), "done": bool(g.get("done"))})
        logger.info(
            "PakistanLawSite case-ID walk tick: %s probes, %s judgments found (%s new), %s misses; groups %s",
            self.stats["probes"], self.stats["hits"], self.stats["staged"], self.stats["misses"], self.stats["groups"],
        )
        return self.stats


async def run_caseid_walk(db, *, probes: Optional[int] = None, redis_client=None, job_id=None) -> Dict[str, Any]:
    if not settings.login_scraping_effective:
        return {"skipped": "login scraping not permitted in this environment"}
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    if source is None:
        return {"skipped": "PakistanLawSite source missing"}
    if source.state in ("HALTED", "DISABLED", "PAUSED") or not source.is_active:
        return {"skipped": f"source state {source.state}"}
    walker = CaseIdWalker(db, source, redis_client=redis_client, job_id=job_id)
    mode = await get_harvest_mode(db)
    walker.pacing_profile = login_pacing_profile(mode)
    walker.pipeline.harvest_mode = mode
    walker.pipeline.pacing_profile = walker.pacing_profile
    active = len([s for s in await walker.pipeline.manager.slots() if s.state == "ACTIVE"])
    lock = SessionLock(SOURCE_NAME, redis_client, max_holders=max(1, min(int(walker.pacing_profile.get("login_session_concurrency") or 1), active)))
    try:
        await lock.acquire()
    except SessionLockHeld:
        await lock.release()
        return {"skipped": "login session lock held"}
    try:
        walker.pipeline._session_lock = lock
        walker.pipeline._assert_permitted()
        return await walker.run(int(probes or settings.PLS_CASEID_WALK_PROBES_PER_TICK))
    except (PacingBudgetExceeded, LoginRequired) as exc:
        await db.rollback()
        logger.info("PakistanLawSite case-ID walk paused: %s", exc)
        return {"paused": str(exc), **walker.stats}
    finally:
        try:
            await walker.pipeline.runner.close()
        except Exception:
            pass
        await lock.release()


@shared_task(name="scraper.tasks.pls_caseid_walk.pls_caseid_walk_tick")
def pls_caseid_walk_tick(**kwargs) -> Dict[str, Any]:
    if not settings.PLS_CASEID_WALK_ENABLED:
        return {"skipped": "PLS_CASEID_WALK_ENABLED is off"}
    from scraper.tasks.deploy_hold import login_work_held

    if login_work_held():
        return {"skipped": "deploy_hold"}

    async def _inner() -> Dict[str, Any]:
        from scraper.heartbeat import beat

        await beat("caseid_walk")
        async with SessionLocal() as db:
            return await run_caseid_walk(db, **kwargs)

    return run_async(_inner())
