"""PakistanLawSite citation-grid progress helpers for health checks and stall detection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.models import Judgment, ScraperJob, ScraperSource

SOURCE_NAME = "PakistanLawSite"


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def citation_grid_cursor_keys(cfg: Dict[str, Any]) -> List[str]:
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
    for shard in (0, 1):
        shard_key = f"citation_grid_cursor_shard_{shard}"
        shard_raw = cfg.get(shard_key)
        if isinstance(shard_raw, dict):
            shard_view = dict(shard_raw)
            shard_offset = _to_int(shard_raw.get("row_offset"))
            shard_view["row_offset"] = shard_offset if shard_offset is not None else 0
            status[shard_key] = shard_view
    last_flush: Dict[str, Any] = {}
    last_start_offset = _to_int(cursor.get("last_start_offset"))
    if last_start_offset is not None:
        last_flush["offset_before"] = last_start_offset
    if row_offset is not None:
        last_flush["offset_after"] = row_offset
    last_take_count = _to_int(cursor.get("last_take_count"))
    if last_take_count is not None:
        last_flush["processed_rows"] = last_take_count
    for field in ("offset_before", "offset_after", "staged_this_flush", "processed_rows"):
        if field in last_flush:
            continue
        parsed = _to_int(cursor.get(field))
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
            continue
        total = _to_int(cur.get("last_total_rows")) or 0
        offset = _to_int(cur.get("row_offset")) or 0
        if total <= 0:
            continue
        if cursor_saturated(cur, now=now):
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


def dispatch_saturated(cfg: Dict[str, Any], reporter_shard: Optional[int], *, now: Optional[datetime] = None) -> bool:
    """Scheduler guard: do not enqueue a job for a shard (or the unsharded walk) whose grid is saturated."""
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
    if window.get("staged"):
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
    for key in citation_grid_cursor_keys(cfg):
        cur = cfg.get(key)
        if not isinstance(cur, dict):
            continue
        shards[key] = {
            "saturated": cursor_saturated(cur, now=now),
            "saturated_at": cur.get("saturated_at"),
            "reason": cur.get("saturated_reason"),
            "lap": cur.get("lap"),
            "last_lap": cur.get("last_lap"),
        }
    return {"shards": shards, "all_saturated": bool(shards) and all(v["saturated"] for v in shards.values()), "recheck_hours": saturated_recheck_hours()}


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
