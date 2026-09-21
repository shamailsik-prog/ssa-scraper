"""Celery entry points for the archive mirror and reconcile_storage (Section 9 / 9A)."""

from __future__ import annotations

from typing import Any, Dict

from celery import shared_task

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.storage.archive import ArchiveMirror


async def mirror_pending(
    limit: int = settings.ARCHIVE_MIRROR_JUDGMENTS_PER_RUN,
    statute_limit: int = settings.ARCHIVE_MIRROR_STATUTES_PER_RUN,
    instrument_limit: int = settings.ARCHIVE_MIRROR_INSTRUMENTS_PER_RUN,
) -> Dict[str, Any]:
    async with SessionLocal() as db:
        mirror = ArchiveMirror(db)
        result = await mirror.mirror_pending(limit)
        statutes = await mirror.mirror_statutes(limit=statute_limit)
        instruments = await mirror.mirror_instruments(limit=instrument_limit)
        await db.commit()
        result["statutes"] = statutes
        result["instruments"] = instruments
        return result


async def reconcile_storage() -> Dict[str, Any]:
    async with SessionLocal() as db:
        report = await ArchiveMirror(db).reconcile()
        await db.commit()
        return report


@shared_task(name="scraper.tasks.archive_mirror.mirror_pending")
def mirror_pending_task(
    limit: int = settings.ARCHIVE_MIRROR_JUDGMENTS_PER_RUN,
    statute_limit: int = settings.ARCHIVE_MIRROR_STATUTES_PER_RUN,
    instrument_limit: int = settings.ARCHIVE_MIRROR_INSTRUMENTS_PER_RUN,
):
    return run_async(mirror_pending(limit=limit, statute_limit=statute_limit, instrument_limit=instrument_limit))


@shared_task(name="scraper.tasks.archive_mirror.reconcile_storage")
def reconcile_storage_task():
    return run_async(reconcile_storage())
