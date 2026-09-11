"""Celery entry points for the archive mirror and reconcile_storage (Section 9 / 9A)."""

from __future__ import annotations

from typing import Any, Dict

from celery import shared_task

from scraper.database import SessionLocal, run_async
from scraper.storage.archive import ArchiveMirror


async def mirror_pending(limit: int = 200) -> Dict[str, Any]:
    async with SessionLocal() as db:
        mirror = ArchiveMirror(db)
        result = await mirror.mirror_pending(limit)
        statutes = await mirror.mirror_statutes()
        instruments = await mirror.mirror_instruments()
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
def mirror_pending_task(limit: int = 200):
    return run_async(mirror_pending(limit))


@shared_task(name="scraper.tasks.archive_mirror.reconcile_storage")
def reconcile_storage_task():
    return run_async(reconcile_storage())
