"""Postgres advisory locks for exclusive harvest runs (B7).

Per login slot: two harvest ticks on the same slot must not run concurrently cluster-wide.
Disabled when SSA_HARVEST_PG_LOCK=0/false.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from scraper.config import settings
from scraper.database import _advisory_lock_database_url, _engine_kwargs

logger = logging.getLogger(__name__)

# Distinct from promotion / backfill / test probe locks.
HARVEST_RUN_LOCK_BASE = 0x55A_9704


def harvest_run_lock_key(slot: int) -> int:
    if slot not in (1, 2):
        raise ValueError(f"harvest slot must be 1 or 2, got {slot}")
    return HARVEST_RUN_LOCK_BASE + int(slot)


@asynccontextmanager
async def harvest_run_pg_lock(slot: int) -> AsyncIterator[bool]:
    """Try to take the harvest advisory lock for ``slot``. Yields True when held."""
    if not settings.SSA_HARVEST_PG_LOCK:
        yield True
        return

    lock_url = _advisory_lock_database_url()
    engine = create_async_engine(lock_url, **_engine_kwargs())
    conn: Optional[AsyncConnection] = None
    acquired = False
    try:
        conn = await engine.connect()
        key = harvest_run_lock_key(slot)
        acquired = bool(
            (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar()
        )
        if acquired:
            await conn.commit()
        yield acquired
    finally:
        if conn is not None:
            try:
                if acquired:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(:k)"),
                        {"k": harvest_run_lock_key(slot)},
                    )
                    await conn.commit()
            except Exception as exc:
                logger.warning("harvest_run_pg_lock release failed slot=%s: %s", slot, exc)
            await conn.close()
        await engine.dispose()
