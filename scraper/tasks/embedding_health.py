"""Embedding-queue health: why nothing drains, and an alert when the queue stalls (audit 2026-09-30: 0 of 19,753
judgments embedded, 27,027 queued, worker logging 'OPENAI_API_KEY NOT CONFIGURED; embeddings idle' every 5 minutes)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.models import EmbeddingQueue, Judgment


def embedding_idle_reason() -> Optional[str]:
    """None when the worker can embed; otherwise the exact reason it will not."""
    if not settings.OPENAI_API_KEY:
        return "OPENAI_API_KEY not configured (no embedding backend)"
    return None


async def embedding_health(db: AsyncSession, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    by_status = {s: int(n) for s, n in (await db.execute(select(EmbeddingQueue.status, func.count()).group_by(EmbeddingQueue.status))).all()}
    oldest = (await db.execute(select(func.min(EmbeddingQueue.created_at)).where(EmbeddingQueue.status == "pending"))).scalar()
    last_done = (await db.execute(select(func.max(EmbeddingQueue.last_attempt_at)).where(EmbeddingQueue.status == "done"))).scalar()
    embedded = int((await db.execute(select(func.count()).select_from(Judgment).where(Judgment.embedding.isnot(None)))).scalar() or 0)
    total = int((await db.execute(select(func.count()).select_from(Judgment))).scalar() or 0)
    idle = embedding_idle_reason()
    blockers = []
    if idle:
        blockers.append(idle)
    if not settings.EMBED_LOGIN_SESSION_ROWS_EXTERNALLY and settings.OPENAI_API_KEY:
        blockers.append("EMBED_LOGIN_SESSION_ROWS_EXTERNALLY=false: login-session (PakistanLawSite) text is not sent to an external model")
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    pending = by_status.get("pending", 0)
    oldest_age_h = round((now - oldest).total_seconds() / 3600.0, 1) if oldest else None
    threshold = float(settings.EMBED_QUEUE_ALERT_HOURS)
    stalled = bool(pending and oldest_age_h is not None and oldest_age_h >= threshold and not (last_done and now - last_done < timedelta(hours=threshold)))
    return {
        "judgments_embedded": embedded,
        "judgments_total": total,
        "queue": by_status,
        "pending": pending,
        "oldest_pending_age_hours": oldest_age_h,
        "last_embedded_at": last_done.isoformat() if last_done else None,
        "draining": bool(pending == 0 or (last_done and now - last_done < timedelta(hours=threshold))),
        "stalled": stalled,
        "blockers": blockers,
    }
