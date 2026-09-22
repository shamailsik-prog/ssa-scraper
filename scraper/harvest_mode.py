"""
Runtime harvest mode controls for scheduler and pacing.

The mode is persisted in corpus_metadata so operators can switch it from the dashboard
without editing `.env` or restarting services.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Literal, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import HARVEST_MODES, settings
from scraper.models import CorpusMetadata, CrawlFrontier, Judgment, ScraperSource, Statute

HarvestMode = Literal["backfill", "updates"]
META_MODE = "harvest_mode"
META_CHANGED_AT = "harvest_mode_changed_at"
META_CHANGED_BY = "harvest_mode_changed_by"
META_REASON = "harvest_mode_reason"


def normalise_mode(raw: str) -> HarvestMode:
    mode = (raw or "").strip().lower()
    if mode not in HARVEST_MODES:
        raise ValueError(f"mode must be one of {HARVEST_MODES}")
    return mode  # type: ignore[return-value]


def login_pacing_profile(mode: HarvestMode) -> Dict[str, Any]:
    if mode == "backfill":
        return {
            "mode": mode,
            "pages_per_hour": settings.BACKFILL_PAGES_PER_HOUR,
            "pages_per_day": settings.BACKFILL_PAGES_PER_DAY,
            "login_delay_min": settings.BACKFILL_LOGIN_DELAY_MIN,
            "login_delay_max": settings.BACKFILL_LOGIN_DELAY_MAX,
            "login_session_concurrency": settings.BACKFILL_LOGIN_SESSION_CONCURRENCY,
        }
    return {
        "mode": mode,
        "pages_per_hour": settings.PAGES_PER_HOUR,
        "pages_per_day": settings.PAGES_PER_DAY,
        "login_delay_min": settings.LOGIN_DELAY_MIN,
        "login_delay_max": settings.LOGIN_DELAY_MAX,
        "login_session_concurrency": settings.LOGIN_SESSION_CONCURRENCY,
    }


def cadence_for_source(source: ScraperSource, mode: HarvestMode) -> int:
    """Return scheduling cadence in minutes for one source in the selected mode."""
    cfg = source.config_json or {}
    if mode == "backfill":
        return max(1, int(cfg.get("backfill_frequency_minutes") or settings.BACKFILL_SOURCE_FREQUENCY_MINUTES))
    return max(1, int(cfg.get("update_frequency_hours") or settings.UPDATE_CADENCE_HOURS) * 60)


def source_selected_for_mode(source: ScraperSource, mode: HarvestMode) -> bool:
    cfg = source.config_json or {}
    if mode == "backfill":
        return bool(cfg.get("backfill_enabled", True))
    return bool(cfg.get("update_enabled", True))


def source_backfill_priority(source: ScraperSource) -> int:
    cfg = source.config_json or {}
    return int(cfg.get("backfill_priority", 100))


async def get_harvest_mode(db: AsyncSession) -> HarvestMode:
    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == META_MODE))).scalars().first()
    if row is None:
        return normalise_mode(settings.HARVEST_MODE)
    try:
        return normalise_mode(row.value)
    except ValueError:
        return normalise_mode(settings.HARVEST_MODE)


async def set_harvest_mode(
    db: AsyncSession,
    mode: HarvestMode,
    *,
    changed_by: str = "system",
    reason: str = "",
) -> None:
    mode = normalise_mode(mode)
    now = datetime.now(timezone.utc).isoformat()
    updates = {
        META_MODE: mode,
        META_CHANGED_AT: now,
        META_CHANGED_BY: (changed_by or "system")[:200],
        META_REASON: (reason or "").strip()[:1000],
    }
    existing = {
        m.key: m
        for m in (
            await db.execute(select(CorpusMetadata).where(CorpusMetadata.key.in_(list(updates.keys()))))
        ).scalars().all()
    }
    for key, value in updates.items():
        if key in existing:
            existing[key].value = value
        else:
            db.add(CorpusMetadata(key=key, value=value))
    await db.flush()


async def backfill_progress(db: AsyncSession, *, source_names: Sequence[str] | None = None) -> Dict[str, Any]:
    frontier_q = select(func.count()).select_from(CrawlFrontier).where(CrawlFrontier.status.in_(["pending", "in_progress", "stale"]))
    if source_names:
        frontier_q = frontier_q.where(CrawlFrontier.source_name.in_(list(source_names)))
    frontier_remaining = int((await db.execute(frontier_q)).scalar() or 0)
    judgments = int((await db.execute(select(func.count()).select_from(Judgment))).scalar() or 0)
    statutes = int((await db.execute(select(func.count()).select_from(Statute))).scalar() or 0)
    target_j = int(settings.BACKFILL_TARGET_JUDGMENTS)
    target_s = int(settings.BACKFILL_TARGET_STATUTES)
    targets_configured = bool(target_j or target_s)
    targets_met = {"judgments": judgments >= target_j if target_j else True, "statutes": statutes >= target_s if target_s else True}
    # Backfill is "complete" only when the firm has stated what complete means (a judgment and/or
    # statute target) and that target is reached with nothing left in the frontier. Without a target
    # an empty frontier proves nothing: at first boot the frontier is empty before any source has run,
    # and the PakistanLawSite citation grid never uses the frontier at all.
    complete = targets_configured and frontier_remaining == 0 and all(targets_met.values())
    blocked_reason = None
    if not targets_configured:
        blocked_reason = "no BACKFILL_TARGET_JUDGMENTS / BACKFILL_TARGET_STATUTES configured; auto-switch stays off"
    elif frontier_remaining:
        blocked_reason = f"{frontier_remaining} frontier rows still pending"
    elif not all(targets_met.values()):
        blocked_reason = "backfill targets not yet met"
    return {
        "frontier_remaining": frontier_remaining,
        "judgments_total": judgments,
        "statutes_total": statutes,
        "judgments_target": target_j or None,
        "statutes_target": target_s or None,
        "targets_configured": targets_configured,
        "targets_met": targets_met,
        "complete": complete,
        "auto_switch_blocked_reason": blocked_reason,
    }


async def selected_source_names(db: AsyncSession, mode: HarvestMode) -> list[str]:
    rows = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.is_active.is_(True), ScraperSource.state.in_(["ACTIVE", "PAUSED", "HALTED"]))
        )
    ).scalars().all()
    return [s.source_name for s in rows if source_selected_for_mode(s, mode)]
