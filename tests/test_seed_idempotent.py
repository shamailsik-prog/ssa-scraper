"""Finding #238: concurrent court seed must not raise uq_court_* (fails on main @ 0580f94 without fixes)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scraper.config import settings
from scraper.database import engine, seed_data

_CONCURRENT_SEED_ROUNDS = 10
_PARALLEL_SEED_CALLS = 8


async def _truncate_court_only() -> None:
    async with engine.begin() as conn:
        await conn.execute(text('TRUNCATE "court" RESTART IDENTITY CASCADE'))


async def _seed_data_on_engine(db_engine) -> None:
    import scraper.database as dbmod

    prior = dbmod.SessionLocal
    dbmod.SessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await seed_data()
    finally:
        dbmod.SessionLocal = prior
        await db_engine.dispose()


async def _gather_seed_data_parallel_engines(n: int) -> list[BaseException | None]:
    async def _one() -> None:
        eng = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0)
        await _seed_data_on_engine(eng)

    results = await asyncio.gather(*[_one() for _ in range(n)], return_exceptions=True)
    return list(results)


@pytest.mark.asyncio
async def test_concurrent_seed_data_no_unique_violations(clean_db):
    """Red on main 0580f94: parallel seed_data() after empty court table races on uq_court_*."""
    try:
        for _ in range(_CONCURRENT_SEED_ROUNDS):
            await _truncate_court_only()
            results = await _gather_seed_data_parallel_engines(_PARALLEL_SEED_CALLS)
            errors = [r for r in results if isinstance(r, BaseException)]
            assert not errors, errors[0] if errors else None
    finally:
        await engine.dispose(close=False)
