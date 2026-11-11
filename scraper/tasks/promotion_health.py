"""Promotion backlog health checks (stale promoted_at with extracted staging waiting)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from celery import shared_task
from sqlalchemy import func, select

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.models import Judgment, Notification, ScraperStaging, StatutesStaging
from scraper.notify import notify

logger = logging.getLogger(__name__)

NOTIFICATION_CODE = "PROMOTION_STALE_QUEUE"


async def evaluate_promotion_stale_alert(db, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not bool(getattr(settings, "PROMOTION_STALE_ALERT_ENABLED", True)):
        return {"alert": False, "skipped": "disabled"}
    promo_idle_min = float(getattr(settings, "PROMOTION_STALE_PROMOTION_MINUTES", 15) or 15)
    extracted_min = float(getattr(settings, "PROMOTION_STALE_EXTRACTED_MIN_AGE_MINUTES", 10) or 10)
    last_promoted = (await db.execute(select(func.max(Judgment.promoted_at)))).scalar()
    if last_promoted is not None and last_promoted.tzinfo is None:
        last_promoted = last_promoted.replace(tzinfo=timezone.utc)
    idle_minutes = None
    if last_promoted is not None:
        idle_minutes = max(0.0, (now - last_promoted).total_seconds() / 60.0)
    extracted_cutoff = now - timedelta(minutes=extracted_min)
    j_pending = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ScraperStaging)
                .where(
                    ScraperStaging.status == "extracted",
                    ScraperStaging.promoted_to_id.is_(None),
                    ScraperStaging.updated_at <= extracted_cutoff,
                )
            )
        ).scalar()
        or 0
    )
    s_pending = int(
        (
            await db.execute(
                select(func.count())
                .select_from(StatutesStaging)
                .where(
                    StatutesStaging.status == "extracted",
                    StatutesStaging.promoted_to_id.is_(None),
                    StatutesStaging.updated_at <= extracted_cutoff,
                )
            )
        ).scalar()
        or 0
    )
    pending = j_pending + s_pending
    alert = pending > 0 and (last_promoted is None or (idle_minutes is not None and idle_minutes >= promo_idle_min))
    result: Dict[str, Any] = {
        "alert": bool(alert),
        "minutes_since_last_promotion": None if idle_minutes is None else round(idle_minutes, 2),
        "pending_extracted_rows": pending,
        "judgment_extracted_waiting": j_pending,
        "statute_extracted_waiting": s_pending,
        "promotion_idle_threshold_minutes": promo_idle_min,
        "extracted_min_age_minutes": extracted_min,
        "last_promoted_at": last_promoted.isoformat() if last_promoted else None,
    }
    if alert:
        open_row = (
            await db.execute(
                select(Notification).where(Notification.code == NOTIFICATION_CODE, Notification.acknowledged.is_(False))
            )
        ).scalars().first()
        if open_row is None:
            await notify(
                db,
                level="warning",
                code=NOTIFICATION_CODE,
                message=(
                    f"Promotion stale: no judgment promoted for {idle_minutes:.0f} min "
                    f"while {pending} extracted staging row(s) are waiting"
                ),
                details=result,
            )
    elif pending == 0 or (idle_minutes is not None and idle_minutes < promo_idle_min):
        for row in (
            await db.execute(
                select(Notification).where(Notification.code == NOTIFICATION_CODE, Notification.acknowledged.is_(False))
            )
        ).scalars().all():
            row.acknowledged = True
    return result


async def promotion_stale_alert_tick() -> Dict[str, Any]:
    async with SessionLocal() as db:
        result = await evaluate_promotion_stale_alert(db)
        await db.commit()
        return result


@shared_task(name="scraper.tasks.promotion_health.promotion_stale_alert")
def promotion_stale_alert_task():
    return run_async(promotion_stale_alert_tick())
