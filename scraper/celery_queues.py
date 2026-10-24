"""Celery queue names and routing helpers (promote isolation rollout)."""

from __future__ import annotations

from scraper.config import settings

PROMOTE_QUEUE = "promote"
MAINTENANCE_QUEUE = "maintenance"


def promote_task_queue() -> str:
    """Queue for promote_staging_records and chain-triggered promotion runs."""
    if bool(getattr(settings, "SSA_PROMOTE_DEDICATED_QUEUE_ENABLED", False)):
        return PROMOTE_QUEUE
    return MAINTENANCE_QUEUE
