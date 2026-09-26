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


def grid_rows_remaining(cfg: Dict[str, Any]) -> int:
    """Approximate rows left across all shard cursors (0 when unknown or complete)."""
    remaining = 0
    for key in citation_grid_cursor_keys(cfg):
        cur = cfg.get(key)
        if not isinstance(cur, dict):
            continue
        total = _to_int(cur.get("last_total_rows")) or 0
        offset = _to_int(cur.get("row_offset")) or 0
        if total <= 0:
            continue
        remaining += max(0, total - offset)
    return remaining


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
