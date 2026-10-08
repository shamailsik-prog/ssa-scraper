"""Celery entry points for the archive mirror and reconcile_storage (Section 9 / 9A)."""

from __future__ import annotations

import time
from typing import Any, Dict

from celery import shared_task
from sqlalchemy import text

from scraper.config import settings
from scraper.database import SessionLocal, engine, run_async
from scraper.storage.archive import ArchiveMirror


MIRROR_LOCK_KEY = 0x55A_A2C1  # pg advisory lock: one mirror run at a time


async def mirror_pending(limit: int = 200) -> Dict[str, Any]:
    """One mirror run at a time, bounded in time and committing as it goes.

    The lock is a session-level advisory lock on its own AUTOCOMMIT connection (as for promotion),
    because the run now commits its ledger every few objects: a second run would otherwise pick the
    same oldest judgments and upload duplicate files (Drive allows two files with one name). After
    MIRROR_RUN_BUDGET_SECONDS the run stops starting new objects, commits, and asks the chain for the
    next run, so a large backlog moves in steady slices and a killed worker loses one slice at most."""
    from scraper.heartbeat import beat
    from scraper.tasks.chain import request_mirror

    await beat("archive_mirror")
    async with engine.connect() as lock_conn:
        lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
        got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": MIRROR_LOCK_KEY})).scalar()
        if not got:
            # The running pass may have chosen its batch before the latest promotion: ask the chain
            # for a follow-up run rather than leave the new records to the next Beat pass.
            request_mirror()
            return {"skipped": "mirror_run_in_progress"}
        try:
            async with SessionLocal() as db:
                mirror = ArchiveMirror(db, deadline=time.monotonic() + settings.MIRROR_RUN_BUDGET_SECONDS, commit_progress=True)
                result = await mirror.mirror_pending(limit)
                statutes = await mirror.mirror_statutes()
                instruments = await mirror.mirror_instruments()
                result["lag_alerts"] = await mirror.alert_on_lag()
                await db.commit()
                result["statutes"] = statutes
                result["instruments"] = instruments
                result["out_of_time"] = mirror.out_of_time
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIRROR_LOCK_KEY})
    if result["out_of_time"]:
        request_mirror()  # more is pending: carry on in the next slice
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
