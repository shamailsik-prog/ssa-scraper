"""Self-healing throughput watchdog for PakistanLawSite (every ~10 minutes, maintenance queue).

When PakistanLawSite is ACTIVE and no judgment was promoted (with search work pending) for
PLS_THROUGHPUT_IDLE_MINUTES, it diagnoses why and fixes what it can without a human:

* login slot not ACTIVE           -> enqueue recover_login_slots (re-login with the saved credentials)
* session lock held, no live job  -> retire the dead job rows and release the stale Redis locks
* browser crash / stuck worker    -> ask the host watchdog (scripts/pls_watchdog_host.sh) to recreate worker-scraper
* search-harvest queue empty      -> seed every journal x year the form offers; requeue transient failures
* nothing ran recently            -> enqueue a search-harvest tick
* search form map missing/stale   -> reported as unresolved (needs a remap)

Every check, action and unresolved reason is written to the PakistanLawSite source config
(pls_throughput_watchdog) and shown on the live dashboard. It also evaluates the PakistanCode
auto-transition (coverage >= PLS_AUTO_UNPAUSE_COVERAGE) and flips it only when
PLS_AUTO_UNPAUSE_PAKISTANCODE is enabled."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from celery import shared_task
from sqlalchemy import func, select

from scraper.config import settings
from scraper.watchdog_settings import wsettings
from scraper.database import SessionLocal, run_async

logger = logging.getLogger(__name__)

SOURCE_NAME = "PakistanLawSite"
WATCHDOG_KEY = "pls_throughput_watchdog"
REQUEST_FILE = "pls_watchdog_request.json"
HISTORY_LIMIT = 30
_CRASH_PATTERNS = re.compile(r"page crashed|target closed|browser has been closed|select_option: timeout|connection closed|disconnected|not visible|timeout [0-9]+ms exceeded", re.I)
_LOGIN_PATTERNS = re.compile(r"login|needs_human|session expired|mainpage", re.I)
_PERMANENT_ERRORS = re.compile(r"not offered|exhausted|cannot express|no usable mapped", re.I)


def state_dir() -> Path:
    return Path(getattr(settings, "STATE_STORAGE_PATH", "") or "/app/state")


def classify_errors(errors: List[str]) -> Dict[str, int]:
    out = {"browser_crash": 0, "login": 0, "other": 0}
    for err in errors:
        if not err:
            continue
        if _CRASH_PATTERNS.search(err):
            out["browser_crash"] += 1
        elif _LOGIN_PATTERNS.search(err):
            out["login"] += 1
        else:
            out["other"] += 1
    return out


def plan_remediation(
    *,
    source_state: Optional[str],
    pages: int,
    judgments: int,
    slot_states: Dict[int, str],
    lock_keys: List[str],
    running_jobs: int,
    stale_running_jobs: int,
    pending_queries: int,
    transient_failed: int,
    search_ran_recently: bool,
    search_harvest_active: bool,
    map_ok: bool,
    recent_errors: List[str],
    idle_checks: int,
    last_recreate_at: Optional[datetime],
    now: datetime,
    recreate_cooldown: timedelta = timedelta(hours=2),
) -> Dict[str, Any]:
    """Pure decision table: what is wrong and what to do about it (no I/O)."""
    if source_state != "ACTIVE":
        return {"state": "skipped", "diagnosis": [f"source state {source_state}"], "actions": [], "unresolved": []}
    # Judgments are the signal, not pages: on Oct 7 every job fetched one page and staged nothing for 17 hours
    # while both slots looked ACTIVE. Pages alone count as progress only when the ledger has nothing left to do.
    if judgments > 0 or (pages > 0 and pending_queries == 0):
        return {"state": "ok", "diagnosis": [f"{pages} pages, {judgments} judgments in window"], "actions": [], "unresolved": []}
    diagnosis: List[str] = [
        f"{pages} PLS page(s) fetched but no judgment promoted in the window" if pages else "no PLS page fetched and no judgment promoted in the window"
    ]
    actions: List[str] = []
    unresolved: List[str] = []
    bad_slots = sorted(n for n, st in slot_states.items() if st != "ACTIVE")
    if bad_slots:
        diagnosis.append(f"login slot(s) not ACTIVE: {', '.join(f'{n}={slot_states[n]}' for n in bad_slots)}")
        actions.append("recover_slots")
    if not any(st == "ACTIVE" for st in slot_states.values()):
        unresolved.append("no ACTIVE login slot; recovery with saved credentials enqueued")
    if lock_keys and stale_running_jobs > 0:
        diagnosis.append(f"session lock held ({len(lock_keys)} key(s)) with {running_jobs} running / {stale_running_jobs} stale job(s)")
        actions.append("release_stale_lock")
    elif lock_keys and running_jobs == 0 and not search_harvest_active:
        diagnosis.append(f"session lock held ({len(lock_keys)} key(s)) with no running scraper job")
        actions.append("release_stale_lock")
    elif stale_running_jobs > 0:
        diagnosis.append(f"{stale_running_jobs} job(s) recorded running without a heartbeat")
        actions.append("release_stale_lock")
    kinds = classify_errors(recent_errors)
    if kinds["browser_crash"]:
        diagnosis.append(f"{kinds['browser_crash']} recent browser crash/timeout error(s)")
    if kinds["login"]:
        diagnosis.append(f"{kinds['login']} recent login-surface error(s)")
        if "recover_slots" not in actions:
            actions.append("recover_slots")
    cooled = last_recreate_at is None or now - last_recreate_at >= recreate_cooldown
    if (kinds["browser_crash"] >= 2 or idle_checks >= 3) and cooled:
        actions.append("recreate_worker")
    if not map_ok:
        diagnosis.append("search form map missing or stale")
        unresolved.append("search form map needs a remap (python -m scraper.tasks.pls_remap_search_form)")
    if pending_queries == 0:
        diagnosis.append("search-harvest queue empty")
        actions.append("seed_queue")
    if transient_failed:
        actions.append("requeue_transient_failures")
    if not search_ran_recently and (pending_queries > 0 or "seed_queue" in actions):
        actions.append("kick_search_harvest")
    return {"state": "idle", "diagnosis": diagnosis, "actions": actions, "unresolved": unresolved}


def append_history(watch: Dict[str, Any], entry: Dict[str, Any], *, limit: int = HISTORY_LIMIT) -> Dict[str, Any]:
    hist = list(watch.get("history") or [])
    hist.insert(0, entry)
    watch["history"] = hist[:limit]
    return watch


async def _lock_keys() -> List[str]:
    import redis.asyncio as aioredis

    from scraper.pls_grid_health import _PLS_LOCK_KEYS

    held: List[str] = []
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        try:
            for key in _PLS_LOCK_KEYS:
                if await r.exists(key):
                    held.append(key)
        finally:
            await r.aclose()
    except Exception:
        pass
    return held


async def _release_locks(keys: List[str]) -> int:
    import redis.asyncio as aioredis

    if not keys:
        return 0
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        return int(await r.delete(*keys) or 0)
    finally:
        await r.aclose()


def _write_request(action: str, reason: str, now: datetime) -> None:
    path = state_dir() / REQUEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"action": action, "reason": reason[:300], "requested_at": now.isoformat()}), encoding="utf-8")


async def run_throughput_watchdog(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    from celery import current_app

    from scraper.auth.session_manager import merge_source_config
    from scraper.models import BrowserSessionSlot, Judgment, PlsSearchHarvestQuery, ScraperJob, ScraperSource, SearchFormMap, SourceProvenance
    from scraper.pls_grid_health import parse_iso

    now = now or datetime.now(timezone.utc)
    window = timedelta(minutes=int(wsettings.PLS_THROUGHPUT_IDLE_MINUTES or 30))
    since = now - window
    async with SessionLocal() as db:
        source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
        if source is None:
            return {"skipped": "no source"}
        cfg = dict(source.config_json or {})
        watch = dict(cfg.get(WATCHDOG_KEY) or {})
        pages = int((await db.execute(select(func.count()).select_from(SourceProvenance).where(SourceProvenance.source_name == SOURCE_NAME, SourceProvenance.fetched_at >= since))).scalar() or 0)
        judgments = int((await db.execute(select(func.count()).select_from(Judgment).where(Judgment.source_name == SOURCE_NAME, Judgment.promoted_at >= since))).scalar() or 0)
        slots = {int(s.slot_number): str(s.state) for s in (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == SOURCE_NAME))).scalars().all()}
        running = (await db.execute(select(ScraperJob).where(ScraperJob.source_name == SOURCE_NAME, ScraperJob.status == "running"))).scalars().all()
        stale_cut = now - timedelta(minutes=int(wsettings.PLS_WATCHDOG_STALE_JOB_MINUTES or 75))
        stale = [j for j in running if (j.updated_at or j.started_at or j.created_at) and (j.updated_at or j.started_at or j.created_at) < stale_cut]
        pending = int((await db.execute(select(func.count()).select_from(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME, PlsSearchHarvestQuery.status.in_(("pending", "in_progress"))))).scalar() or 0)
        harvest_in_progress = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PlsSearchHarvestQuery)
                    .where(PlsSearchHarvestQuery.source_name == SOURCE_NAME, PlsSearchHarvestQuery.status == "in_progress")
                )
            ).scalar()
            or 0
        )
        from scraper.tasks.pls_search_harvest import pls_search_tick_running

        search_harvest_active = harvest_in_progress > 0 or await pls_search_tick_running()
        failed_rows = (await db.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME, PlsSearchHarvestQuery.status == "failed"))).scalars().all()
        transient = [q for q in failed_rows if not _PERMANENT_ERRORS.search(q.last_error or "") and (q.last_run_at is None or q.last_run_at < now - timedelta(hours=6))]
        last_query = (await db.execute(select(func.max(PlsSearchHarvestQuery.last_run_at)).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME))).scalar()
        m = (await db.execute(select(SearchFormMap).where(SearchFormMap.source_name == SOURCE_NAME, SearchFormMap.is_active.is_(True)).order_by(SearchFormMap.created_at.desc()).limit(1))).scalars().first()
        recent_jobs = (await db.execute(select(ScraperJob).where(ScraperJob.source_name == SOURCE_NAME, ScraperJob.created_at >= now - timedelta(hours=2)).order_by(ScraperJob.created_at.desc()).limit(8))).scalars().all()
        errors = [str((j.result_summary or {}).get("stop_reason") or j.error_message or "")[:300] for j in recent_jobs]
        errors += [str(q.last_error or "")[:300] for q in (await db.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME, PlsSearchHarvestQuery.last_run_at >= now - timedelta(hours=2), PlsSearchHarvestQuery.last_error.isnot(None)))).scalars().all()]
        lock_keys = await _lock_keys()
        idle_checks = int(watch.get("consecutive_idle_checks") or 0)
        plan = plan_remediation(
            source_state=source.state,
            pages=pages,
            judgments=judgments,
            slot_states=slots,
            lock_keys=lock_keys,
            running_jobs=len(running),
            stale_running_jobs=len(stale),
            pending_queries=pending,
            transient_failed=len(transient),
            search_ran_recently=bool(last_query and last_query >= since),
            search_harvest_active=search_harvest_active,
            map_ok=bool(m is not None and not m.stale and "reporter" in (m.fields or {})),
            recent_errors=errors,
            idle_checks=idle_checks + (1 if source.state == "ACTIVE" and judgments == 0 and (pages == 0 or pending > 0) else 0),
            last_recreate_at=parse_iso(watch.get("last_recreate_requested_at")),
            now=now,
        )
        done: List[str] = []
        for action in plan["actions"]:
            try:
                if action == "recover_slots":
                    current_app.send_task("scraper.tasks.login_recovery.recover_login_slots", queue="login_session")
                    done.append("enqueued login-slot recovery (saved credentials)")
                elif action == "release_stale_lock":
                    for job in stale:
                        job.status = "interrupted"
                        job.finished_at = now
                        job.error_message = "throughput watchdog: no heartbeat; interrupted so the next job can run"
                    # Only drop the locks when no live job could still own them.
                    n = (
                        await _release_locks(lock_keys)
                        if len(stale) == len(running) and not search_harvest_active
                        else 0
                    )
                    done.append(f"released {n} stale lock key(s), interrupted {len(stale)} dead job(s)")
                elif action == "recreate_worker":
                    _write_request("recreate_worker_scraper", "; ".join(plan["diagnosis"]), now)
                    watch["last_recreate_requested_at"] = now.isoformat()
                    done.append("asked host watchdog to recreate worker-scraper")
                elif action == "seed_queue":
                    from scraper.tasks.pls_search_harvest import seed_search_harvest_plan

                    out = await seed_search_harvest_plan(db)
                    done.append(f"seeded search-harvest queue: {out.get('inserted', 0)} journal x year queries")
                elif action == "requeue_transient_failures":
                    for q in transient:
                        q.status = "pending"
                        cur = dict(q.cursor_json or {})
                        cur["failures"] = 0
                        q.cursor_json = cur
                    done.append(f"requeued {len(transient)} transiently failed search queries")
                elif action == "kick_search_harvest":
                    last_kick = parse_iso(watch.get("last_kick_at"))
                    if last_kick is not None and now - last_kick < window:
                        continue
                    watch["last_kick_at"] = now.isoformat()
                    current_app.send_task("scraper.tasks.pls_search_harvest.pls_search_harvest_tick", kwargs={"priority_gaps": True}, queue="login_session")
                    done.append("enqueued a search-harvest tick")
            except Exception as exc:  # one failed remedy must not stop the others or the record
                plan["unresolved"].append(f"{action} failed: {type(exc).__name__}: {exc}"[:300])
        # Daily ledger upkeep: new years / journals, citation snowball, slow re-check of recent done cells.
        last_upkeep = parse_iso(watch.get("last_upkeep_at"))
        if last_upkeep is None or now - last_upkeep >= timedelta(hours=24):
            try:
                done.extend(await ledger_upkeep(db, now=now))
                watch["last_upkeep_at"] = now.isoformat()
            except Exception as exc:
                plan["unresolved"].append(f"ledger upkeep failed: {type(exc).__name__}: {exc}"[:300])
        # PakistanCode auto-transition (coded switch; acts only when enabled)
        transition: Dict[str, Any] = {}
        try:
            from scraper.coverage import coverage_payload

            cov = await coverage_payload(db, now=now)
            transition = dict(cov.get("pakistancode_transition") or {})
            if transition.get("ready") and transition.get("enabled") and not cfg.get("pakistancode_auto_unpaused_at"):
                pc = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
                if pc is not None and pc.state == "PAUSED":
                    pc.state = "ACTIVE"
                    pc.state_reason = f"auto-unpaused by PLS watchdog: citation coverage {transition['coverage']:.1%} >= {transition['threshold']:.0%}"
                    await merge_source_config(db, source, {"pakistancode_auto_unpaused_at": now.isoformat()})
                    done.append("PakistanCode statutes harvest auto-unpaused (coverage threshold reached)")
        except Exception as exc:
            transition = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        idle = plan["state"] == "idle"
        watch.update(
            {
                "checked_at": now.isoformat(),
                "state": plan["state"],
                "window_minutes": int(window.total_seconds() // 60),
                "pages_in_window": pages,
                "judgments_in_window": judgments,
                "consecutive_idle_checks": (idle_checks + 1) if idle else 0,
                "diagnosis": plan["diagnosis"],
                "actions": done,
                "unresolved": plan["unresolved"],
                "pakistancode_transition": transition,
            }
        )
        if idle or done:
            append_history(watch, {"at": now.isoformat(), "state": plan["state"], "diagnosis": plan["diagnosis"], "actions": done, "unresolved": plan["unresolved"]})
        if done:
            watch["last_action_at"] = now.isoformat()
            watch["last_action"] = "; ".join(done)[:500]
        await merge_source_config(db, source, {WATCHDOG_KEY: watch})
        await db.commit()
        return {k: watch[k] for k in ("state", "diagnosis", "actions", "unresolved", "consecutive_idle_checks")}


def recheck_years(now: datetime) -> List[int]:
    """Years whose finished cells are searched again on the slow schedule (new judgments still appear)."""
    return [now.year, now.year - 1]


async def ledger_upkeep(db, *, now: datetime) -> List[str]:
    from scraper.models import PlsSearchHarvestQuery
    from scraper.tasks.pls_search_harvest import seed_search_harvest_plan, seed_snowball

    notes: List[str] = []
    seeded = await seed_search_harvest_plan(db)
    if seeded.get("inserted"):
        notes.append(f"ledger: added {seeded['inserted']} journal x year cells")
    snow = await seed_snowball(db, limit=2000)
    if snow.get("queries_added"):
        notes.append(f"snowball: queued {snow['queries_added']} cited citations missing from the corpus")
    cutoff = now - timedelta(days=int(wsettings.PLS_SEARCH_RECHECK_DONE_DAYS or 7))
    years = recheck_years(now)
    rows = (
        await db.execute(
            select(PlsSearchHarvestQuery).where(
                PlsSearchHarvestQuery.source_name == SOURCE_NAME,
                PlsSearchHarvestQuery.status == "done",
                PlsSearchHarvestQuery.last_run_at < cutoff,
            )
        )
    ).scalars().all()
    n = 0
    for q in rows:
        qj = dict(q.query_json or {})
        if set(qj) == {"reporter", "year"} and int(qj.get("year") or 0) in years:
            q.status = "pending"
            q.cursor_json = {"page": 1, "row_index": 0}
            q.pages_enumerated = 0
            q.rows_seen = 0
            q.rows_known = 0
            n += 1
    if n:
        notes.append(f"re-check: {n} recent done cells queued again")
    return notes


@shared_task(name="scraper.tasks.pls_throughput_watchdog.pls_throughput_watchdog")
def pls_throughput_watchdog() -> Dict[str, Any]:
    return run_async(run_throughput_watchdog())
