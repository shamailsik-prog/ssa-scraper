"""Finding #238: concurrent court seed must not raise uq_court_* (fails on main @ 0580f94 without fixes)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from scraper.database import engine, seed_data

_CONCURRENT_SEED_ROUNDS = 10
_PARALLEL_SEED_CALLS = 4


async def _truncate_court_only() -> None:
    async with engine.begin() as conn:
        await conn.execute(text('TRUNCATE "court" RESTART IDENTITY CASCADE'))


async def _gather_seed_data() -> list[BaseException | None]:
    results = await asyncio.gather(
        *[seed_data() for _ in range(_PARALLEL_SEED_CALLS)],
        return_exceptions=True,
    )
    return list(results)


@pytest.mark.asyncio
async def test_concurrent_seed_data_no_unique_violations(clean_db):
    """Red on main 0580f94: parallel seed_data() after empty court table races on uq_court_*."""
    for _ in range(_CONCURRENT_SEED_ROUNDS):
        await _truncate_court_only()
        results = await _gather_seed_data()
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, errors[0] if errors else None
