"""9 October 2026: a mirror connection was left "idle in transaction" for over an hour holding row locks on
archive_targets / archive_objects; every later mirror run and reconcile_storage queued behind it, so Google Drive
mirroring stopped. Postgres now ends any app session left idle inside a transaction for
DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS (the dispatcher's 30-minute heartbeat rule: no live job idles that long)."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scraper import database
from scraper.config import settings


async def test_app_sessions_carry_the_idle_in_transaction_timeout():
    async with database.engine.connect() as conn:
        value = (await conn.execute(text("SHOW idle_in_transaction_session_timeout"))).scalar()
    assert value == f"{settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS}s" or value == "30min"


async def test_a_leaked_transaction_is_ended_and_its_locks_released(monkeypatch):
    monkeypatch.setattr(settings, "DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS", 1)
    leaky = create_async_engine(settings.DATABASE_URL, connect_args=database.connect_args())
    try:
        conn = await leaky.connect()
        await conn.execute(text("SELECT pg_advisory_xact_lock(424242)"))  # held until the transaction ends
        await asyncio.sleep(2.5)  # the holder goes quiet, as the leaked mirror connection did
        async with database.engine.connect() as other:
            got = (await other.execute(text("SELECT pg_try_advisory_xact_lock(424242)"))).scalar()
            await other.rollback()
        assert got is True
        await conn.invalidate()
    finally:
        await leaky.dispose()
