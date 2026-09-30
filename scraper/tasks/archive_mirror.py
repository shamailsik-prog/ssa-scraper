"""Celery entry points for the archive mirror and reconcile_storage (Section 9 / 9A)."""

from __future__ import annotations

from typing import Any, Dict

from celery import shared_task
from sqlalchemy import text

from scraper.database import SessionLocal, run_async
from scraper.storage.archive import ArchiveMirror


MIRROR_LOCK_KEY = 0x55A_A2C1  # pg advisory lock: one mirror run at a time


async def mirror_pending(limit: int = 200) -> Dict[str, Any]:
    from scraper.heartbeat import beat

    await beat("archive_mirror")
    async with SessionLocal() as db:
        # A backlog run on Google Drive can outlast the Beat interval. A second run would pick the same
        # oldest judgments, upload duplicate files (Drive allows two files with one name) and then block
        # on the first run's archive_targets row lock. The run is one transaction, so an xact lock covers it.
        got = (await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": MIRROR_LOCK_KEY})).scalar()
        if not got:
            return {"skipped": "mirror_run_in_progress"}
        mirror = ArchiveMirror(db)
        result = await mirror.mirror_pending(limit)
        statutes = await mirror.mirror_statutes()
        instruments = await mirror.mirror_instruments()
        result["lag_alerts"] = await mirror.alert_on_lag()
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
