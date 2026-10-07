"""PakistanLawSite citation-grid progress helpers for health checks and stall detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.models import Judgment, ScraperJob, ScraperSource

SOURCE_NAME = "PakistanLawSite"
PLS_STALL_WATCHDOG_KEY = "pls_stall_watchdog"


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


JOURNAL_CURSOR_PREFIX = "citation_grid_cursor_journal_"
JOURNAL_ROTATION_OFF_KEY = "citation_grid_journal_rotation_off"


def journal_cursor_key(journal: str) -> str:
    """config_json key of one journal's grid cursor ("PCrLJ" -> citation_grid_cursor_journal_PCrLJ)."""
    import re as _re

    return JOURNAL_CURSOR_PREFIX + _re.sub(r"[^A-Za-z0-9]", "", journal or "")


def journal_rotation_off(cfg: Dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    """True while the last probe found no usable journal dropdown (rechecked after PLS_SATURATED_RECHECK_HOURS)."""
    off = cfg.get(JOURNAL_ROTATION_OFF_KEY)
    at = parse_iso(off.get("at")) if isinstance(off, dict) else None
    if at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - at).total_seconds() < saturated_recheck_hours() * 3600


def journal_unsupported(cur: Any, *, now: Optional[datetime] = None) -> bool:
    """True while a journal's grid did not carry that journal's citations (the form ignored the choice)."""
    if not isinstance(cur, dict):
        return False
    at = parse_iso(cur.get("unsupported_at"))
    if at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - at).total_seconds() < saturated_recheck_hours() * 3600


def journal_cursor_keys(cfg: Dict[str, Any]) -> List[str]:
    return sorted(k for k in cfg if isinstance(k, str) and k.startswith(JOURNAL_CURSOR_PREFIX))


def journals_for_shard(reporter_shard: Optional[int]) -> List[str]:
    """The configured journals a reporter shard walks (the pipeline splits the subscribed reporters the same way)."""
    import re as _re

    from scraper.tasks.pakistanlawsite import split_reporter_shards  # lazy: that module imports this one

    journals = list(settings.grid_journals)
    if reporter_shard not in (0, 1):
        return journals
    left, right = split_reporter_shards(settings.subscribed_reporters)
    allowed = {_re.sub(r"[^a-z0-9]", "", r.lower()) for r in (left if reporter_shard == 0 else right)}
    return [j for j in journals if _re.sub(r"[^a-z0-9]", "", j.lower()) in allowed]


def _journal_cursor_started(cur: Any) -> bool:
    if not isinstance(cur, dict):
        return False
    if cur.get("lap") or cur.get("last_lap"):
        return True
    return _to_int(cur.get("row_offset")) not in (None, 0)


def journal_grid_idle(cfg: Dict[str, Any], journal: str, *, now: Optional[datetime] = None) -> bool:
    """True when this journal's grid walk is finished, unsupported, or need not run yet.

    A journal with no cursor progress inherits saturation from the default citation_grid_cursor when
    that walk already finished a full lap with nothing new (production had empty journal cursors
    while the main grid was saturated, which blocked dispatch_saturated and kept enqueueing journal
    jobs that failed on the hidden dashboard Citation Search panel)."""
    cur = cfg.get(journal_cursor_key(journal))
    if isinstance(cur, dict) and (cursor_saturated(cur, now=now) or journal_unsupported(cur, now=now)):
        return True
    main = cfg.get("citation_grid_cursor")
    if isinstance(main, dict) and cursor_saturated(main, now=now):
        total = _to_int(main.get("saturated_total_rows")) or _to_int(main.get("last_total_rows")) or 0
        if total > 0 and not _journal_cursor_started(cur):
            return True
    return False


def journals_all_done(cfg: Dict[str, Any], journals: List[str], *, now: Optional[datetime] = None) -> bool:
    """Every configured journal has a cursor and each is saturated or unsupported (nothing left to walk)."""
    if not journals:
        return False
    return all(journal_grid_idle(cfg, journal, now=now) for journal in journals)


def citation_grid_cursor_keys(cfg: Dict[str, Any]) -> List[str]:
    if settings.PLS_GRID_JOURNAL_ROTATION and settings.grid_journals and not journal_rotation_off(cfg):
        return [journal_cursor_key(j) for j in settings.grid_journals]
    journal_keys = journal_cursor_keys(cfg)
    if journal_keys and settings.PLS_GRID_JOURNAL_ROTATION and not journal_rotation_off(cfg):
        return journal_keys
    shard_keys = [k for k in cfg if k.startswith("citation_grid_cursor_shard_")]
    if shard_keys:
        return sorted(shard_keys)
    return ["citation_grid_cursor"]


def citation_grid_progress_view(source_name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    cursor_raw = cfg.get("citation_grid_cursor")
    cursor = dict(cursor_raw) if isinstance(cursor_raw, dict) else {}
    row_offset = _to_int(cursor.get("row_offset"))
    cursor_view = dict(cursor)
    cursor_view["row_offset"] = row_offset if row_offset is not None else 0
    status: Dict[str, Any] = {
        "source_name": source_name,
        "job_key": "PakistanLawSite:archivedpatientGrid",
        "citation_grid_cursor": cursor_view,
    }
    journal_keys = [
        k
        for k in citation_grid_cursor_keys(cfg)
        if isinstance(k, str) and k.startswith(JOURNAL_CURSOR_PREFIX)
    ]
    for journal_key in journal_keys:
        journal_raw = cfg.get(journal_key)
        if isinstance(journal_raw, dict):
            journal_view = dict(journal_raw)
            journal_offset = _to_int(journal_raw.get("row_offset"))
            journal_view["row_offset"] = journal_offset if journal_offset is not None else 0
            status[journal_key] = journal_view
    for shard in (0, 1):
        shard_key = f"citation_grid_cursor_shard_{shard}"
        shard_raw = cfg.get(shard_key)
        if isinstance(shard_raw, dict):
            shard_view = dict(shard_raw)
            shard_offset = _to_int(shard_raw.get("row_offset"))
            shard_view["row_offset"] = shard_offset if shard_offset is not None else 0
            status[shard_key] = shard_view
    flush_cursor = cursor
    active_journal = cfg.get("citation_grid_journal")
    if active_journal:
        active_key = journal_cursor_key(str(active_journal))
        active_raw = cfg.get(active_key)
        if isinstance(active_raw, dict):
            flush_cursor = active_raw
    last_flush: Dict[str, Any] = {}
    last_start_offset = _to_int(flush_cursor.get("last_start_offset"))
    if last_start_offset is not None:
        last_flush["offset_before"] = last_start_offset
    flush_offset = _to_int(flush_cursor.get("row_offset"))
    if flush_offset is not None:
        last_flush["offset_after"] = flush_offset
    last_take_count = _to_int(flush_cursor.get("last_take_count"))
    if last_take_count is not None:
        last_flush["processed_rows"] = last_take_count
    for field in ("offset_before", "offset_after", "staged_this_flush", "processed_rows"):
        if field in last_flush:
            continue
        parsed = _to_int(flush_cursor.get(field))
        if parsed is not None:
            last_flush[field] = parsed
    if last_flush:
        status["last_flush"] = last_flush
    return status


def grid_rows_remaining(cfg: Dict[str, Any], *, now: Optional[datetime] = None) -> int:
    """Rows left in the current lap of the grid (0 when unknown, complete or saturated).

    Every shard walks the SAME grid (a shard only decides which rows it detail-fetches), so the
    shards' remainders must not be added up: two shards on a 20,568 row grid used to report
    32,336 "remaining" rows, more than the grid holds. The figure is the largest remainder of any
    shard that still has work; a shard whose last full lap found nothing new is saturated and
    counts as 0 until its recheck is due."""
    remaining = 0
    for key in citation_grid_cursor_keys(cfg):
        cur = cfg.get(key)
        if not isinstance(cur, dict):
            remaining = max(remaining, 1)
            continue
        total = _to_int(cur.get("last_total_rows")) or 0
        offset = _to_int(cur.get("row_offset")) or 0
        if total <= 0:
            remaining = max(remaining, 1)
            continue
        if cursor_saturated(cur, now=now) or journal_unsupported(cur, now=now):
            continue
        remaining = max(remaining, max(0, min(total, total - offset)))
    return remaining


# --------------------------------------------------------------------------- saturation
def saturated_recheck_hours() -> float:
    return float(getattr(settings, "PLS_SATURATED_RECHECK_HOURS", 12) or 12)


def cursor_saturated(cur: Any, *, now: Optional[datetime] = None) -> bool:
    """True while a cursor's last COMPLETE lap of the grid staged nothing new and its recheck is not due.

    Re-walking a saturated shard costs a page load per window and produces nothing (the audit of
    2026-09-30 saw 20,568 rows scanned for 0 staged), so the scheduler leaves it alone until the
    recheck interval has passed."""
    if not isinstance(cur, dict):
        return False
    at = parse_iso(cur.get("saturated_at"))
    if at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - at).total_seconds() < saturated_recheck_hours() * 3600


def grid_cursor_key_for_shard(reporter_shard: Optional[int]) -> str:
    return f"citation_grid_cursor_shard_{reporter_shard}" if reporter_shard in (0, 1) else "citation_grid_cursor"


SEARCH_HARVEST_PRIORITY_STALL_REASONS = frozenset({"no_output_while_harvesting", "grid_saturated"})


def defer_pls_grid_for_search_harvest(cfg: Dict[str, Any], *, stall_reason: Optional[str] = None) -> bool:
    """When promotion is stalled or the grid is saturated, yield the login queue to search-harvest gaps."""
    if stall_reason is not None:
        return stall_reason in SEARCH_HARVEST_PRIORITY_STALL_REASONS
    watch = dict(cfg.get(PLS_STALL_WATCHDOG_KEY) or {})
    return bool(watch.get("stalled")) and watch.get("stalled_reason") in SEARCH_HARVEST_PRIORITY_STALL_REASONS


def pls_search_harvest_may_run(
    cfg: Dict[str, Any], *, pending_gaps: int, stall_reason: Optional[str] = None
) -> bool:
    """Beat may schedule ticks when enabled; during a promotion stall, one gap query may run anyway."""
    if getattr(settings, "PLS_SEARCH_HARVEST_ENABLED", False):
        return True
    return pending_gaps > 0 and defer_pls_grid_for_search_harvest(cfg, stall_reason=stall_reason)


PLS_PREEMPT_GRID_JOB_FOR_SEARCH_HARVEST_AFTER = timedelta(
    minutes=int(getattr(settings, "PLS_PREEMPT_GRID_JOB_MINUTES", 10) or 10)
)


def pls_preempt_running_job_for_search_harvest(
    job: ScraperJob,
    cfg: Dict[str, Any],
    *,
    pending_gaps: int,
    stall_reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """True when a running citation-grid job blocks search-harvest gap recovery during a promotion stall."""
    if pending_gaps <= 0 or not defer_pls_grid_for_search_harvest(cfg, stall_reason=stall_reason):
        return False
    if job.status != "running" or job.job_type != "scrape":
        return False
    summary = dict(job.result_summary or {})
    if summary.get("search_harvest") or summary.get("query_key"):
        return False
    staged = int(job.records_extracted or summary.get("staged") or 0)
    if staged > 0:
        return False
    # Citation-grid laps charge pages while staging nothing; they still monopolize the login lock.
    if summary.get("surface_mode") == "citation_grid":
        pass
    else:
        pages = int(job.pages_scraped or summary.get("pages_charged") or summary.get("pages") or 0)
        if pages > 0:
            return False
    started = job.started_at or job.created_at
    if started is None:
        return True
    now = now or datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (now - started) >= PLS_PREEMPT_GRID_JOB_FOR_SEARCH_HARVEST_AFTER


def dispatch_saturated(cfg: Dict[str, Any], reporter_shard: Optional[int], *, now: Optional[datetime] = None) -> bool:
    """Scheduler guard: do not enqueue a job for a shard (or the unsharded walk) whose grid is saturated.

    With the per-journal walk on, the default grid's cursor says nothing about the other journals: the
    walk is saturated only when every configured journal is saturated or unsupported."""
    if settings.PLS_GRID_JOURNAL_ROTATION and settings.grid_journals and not journal_rotation_off(cfg, now=now):
        return journals_all_done(cfg, journals_for_shard(reporter_shard), now=now)
    return cursor_saturated(cfg.get(grid_cursor_key_for_shard(reporter_shard)), now=now)


def lap_update(cur: Dict[str, Any], *, total_rows: int, window: Dict[str, int], now: datetime) -> Dict[str, Any]:
    """Fold one finished window into the cursor's lap ledger and judge saturation once a lap is full.

    A lap is `total_rows` processed rows (the walk is cyclic, so the start offset does not matter).
    Counters: rows, in_shard (rows this shard may fetch), fetched (detail pages), staged (NEW staging
    rows), known (already a judgment), staged_skips (already staged), url_less. When a lap ends with
    staged == 0 the cursor is marked saturated; any new staging clears the mark."""
    lap = dict(cur.get("lap") or {})
    for k in ("rows", "in_shard", "fetched", "staged", "known", "staged_skips", "url_less", "grid_duplicates"):
        lap[k] = int(lap.get(k) or 0) + int(window.get(k) or 0)
    lap.setdefault("started_at", now.isoformat())
    lap["total_rows"] = total_rows
    grew = total_rows > int(cur.get("saturated_total_rows") or 0) > 0
    if window.get("staged") or grew:
        # new work, or the site listed more rows than the lap that found nothing: no longer saturated
        cur.pop("saturated_at", None)
        cur.pop("saturated_reason", None)
    if total_rows > 0 and lap["rows"] >= total_rows:
        done = dict(lap)
        done["finished_at"] = now.isoformat()
        cur["last_lap"] = done
        if int(done.get("staged") or 0) == 0:
            cur["saturated_at"] = now.isoformat()
            cur["saturated_total_rows"] = total_rows
            cur["saturated_reason"] = (
                f"full lap of {done['rows']} rows staged 0 new "
                f"(in_shard={done.get('in_shard')}, known={done.get('known')}, already_staged={done.get('staged_skips')}, "
                f"fetched={done.get('fetched')}, url_less={done.get('url_less')})"
            )
        else:
            cur.pop("saturated_at", None)
            cur.pop("saturated_reason", None)
        lap = {"rows": 0, "in_shard": 0, "fetched": 0, "staged": 0, "known": 0, "staged_skips": 0, "url_less": 0, "grid_duplicates": 0, "started_at": now.isoformat(), "total_rows": total_rows}
    cur["lap"] = lap
    return cur


def grid_saturation_view(cfg: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    shards: Dict[str, Any] = {}
    keys = citation_grid_cursor_keys(cfg)
    for key in keys:
        cur = cfg.get(key)
        if not isinstance(cur, dict):
            shards[key] = {"saturated": False, "saturated_at": None, "reason": None, "lap": None, "last_lap": None}
            continue
        idle = cursor_saturated(cur, now=now) or journal_unsupported(cur, now=now)
        shards[key] = {
            "saturated": idle,
            "saturated_at": cur.get("saturated_at"),
            "reason": cur.get("saturated_reason") or cur.get("unsupported_reason"),
            "lap": cur.get("lap"),
            "last_lap": cur.get("last_lap"),
        }
    all_idle = bool(keys) and all(
        isinstance(cfg.get(key), dict)
        and (cursor_saturated(cfg.get(key), now=now) or journal_unsupported(cfg.get(key), now=now))
        for key in keys
    )
    return {"shards": shards, "all_saturated": all_idle, "recheck_hours": saturated_recheck_hours()}


# --------------------------------------------------------------------------- stalled alarm
def stall_threshold_hours() -> float:
    return float(getattr(settings, "PLS_STALL_NO_PROMOTION_HOURS", 6) or 6)


def compute_stalled(
    *,
    last_promotion_at: Optional[datetime],
    now: Optional[datetime] = None,
    threshold_hours: Optional[float] = None,
    saturation: Optional[Dict[str, Any]] = None,
    harvest_paused: bool = False,
) -> Dict[str, Any]:
    """The alarm the audit asked for: stalled == no new judgment for `threshold_hours`, computed from the
    corpus itself (never from cursors, which advance while nothing is produced).

    `reason` says why: harvest_paused (operator), grid_saturated (every shard finished a full lap with
    nothing new: the site has nothing more for the subscribed reporters), or no_output_while_harvesting
    (a real fault: work is running and yields nothing)."""
    now = now or datetime.now(timezone.utc)
    threshold = float(threshold_hours if threshold_hours is not None else stall_threshold_hours())
    if last_promotion_at is not None and last_promotion_at.tzinfo is None:
        last_promotion_at = last_promotion_at.replace(tzinfo=timezone.utc)
    idle_hours = None if last_promotion_at is None else max(0.0, (now - last_promotion_at).total_seconds() / 3600.0)
    stalled = idle_hours is None or idle_hours >= threshold
    reason = None
    if stalled:
        if harvest_paused:
            reason = "harvest_paused"
        elif saturation and saturation.get("all_saturated"):
            reason = "grid_saturated"
        else:
            reason = "no_output_while_harvesting"
    return {
        "stalled": bool(stalled),
        "stalled_reason": reason,
        "hours_since_last_judgment": None if idle_hours is None else round(idle_hours, 2),
        "threshold_hours": threshold,
        "last_judgment_at": last_promotion_at.isoformat() if last_promotion_at else None,
    }


def grid_total_rows(cfg: Dict[str, Any]) -> int:
    """The grid's size as the latest window saw it, over every cursor (0 when unknown)."""
    keys = ("citation_grid_cursor", *citation_grid_cursor_keys(cfg))
    return max([_to_int((cfg.get(k) or {}).get("last_total_rows")) or 0 for k in keys if isinstance(cfg.get(k), dict)] or [0])


def grid_harvest_incomplete(cfg: Dict[str, Any]) -> bool:
    return grid_rows_remaining(cfg) > 0


def pls_zero_query_grid_failure(stats: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[str]:
    if stats.get("skipped") or stats.get("paused") or stats.get("halted") or stats.get("pacing_paused"):
        return None
    if stats.get("citation_grid_seek_failed"):
        return "citation-grid seek failed; cursor was not advanced"
    if stats.get("surface_mode") != "citation_grid":
        return None
    queries = int(stats.get("queries") or 0)
    windows = int(stats.get("citation_grid_windows") or 0)
    pages = int(stats.get("pages_charged") or stats.get("pages") or 0)
    if queries > 0 or windows > 0 or pages > 0:
        return None
    remaining = grid_rows_remaining(cfg)
    if remaining <= 0:
        return None
    return f"zero citation-grid progress with {remaining} grid rows remaining"


def pls_stall_verdict_for_config(
    cfg: Dict[str, Any],
    *,
    last_promotion_at: Optional[datetime],
    source_state: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Live promotion stall verdict from corpus + grid saturation (not the cached pls_stall_watchdog blob)."""
    now = now or datetime.now(timezone.utc)
    saturation = grid_saturation_view(cfg, now=now)
    paused = source_state != "ACTIVE" or bool(cfg.get("paused_by_admin"))
    return compute_stalled(
        last_promotion_at=last_promotion_at,
        now=now,
        saturation=saturation,
        harvest_paused=paused,
    )


async def pls_judgment_counts(db: AsyncSession) -> Tuple[int, Dict[str, int]]:
    total = int((await db.execute(select(func.count()).select_from(Judgment))).scalar() or 0)
    by_source = {
        name: int(n)
        for name, n in (await db.execute(select(Judgment.source_name, func.count()).group_by(Judgment.source_name))).all()
    }
    return total, by_source


async def pls_last_judgment_at(db: AsyncSession) -> Optional[datetime]:
    return (await db.execute(select(func.max(Judgment.promoted_at)).where(Judgment.source_name == SOURCE_NAME))).scalar()


_PLS_LOCK_KEYS = (
    "corpus:login_session_lock:PakistanLawSite",
    "corpus:login_session_lock:PakistanLawSite:holders",
    "corpus:login_session_lock:PakistanLawSite:slot1",
    "corpus:login_session_lock:PakistanLawSite:slot2",
)


async def pls_harvest_in_progress(db: AsyncSession) -> Optional[str]:
    """Return a short reason when a PLS harvest/login job holds the session lock or is running."""
    import redis.asyncio as aioredis

    try:
        r = aioredis.from_url(settings.REDIS_URL)
        try:
            for key in _PLS_LOCK_KEYS:
                if await r.exists(key):
                    return f"redis:{key}"
        finally:
            await r.aclose()
    except Exception:
        pass
    running = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ScraperJob)
                .where(ScraperJob.source_name == SOURCE_NAME, ScraperJob.status == "running")
            )
        ).scalar()
        or 0
    )
    if running > 0:
        return f"running_jobs:{running}"
    return None


async def pls_source_config(db: AsyncSession) -> Dict[str, Any]:
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))
    ).scalars().first()
    return dict(source.config_json or {}) if source is not None else {}


def stall_signature(cfg: Dict[str, Any], judgment_count: int) -> Dict[str, Any]:
    sig: Dict[str, Any] = {"judgments": judgment_count, "cursors": {}}
    for key in citation_grid_cursor_keys(cfg):
        cur = cfg.get(key)
        if isinstance(cur, dict):
            sig["cursors"][key] = {
                "row_offset": _to_int(cur.get("row_offset")) or 0,
                "last_total_rows": _to_int(cur.get("last_total_rows")) or 0,
                "updated_at": cur.get("updated_at"),
            }
    return sig


def signatures_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return a == b


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
