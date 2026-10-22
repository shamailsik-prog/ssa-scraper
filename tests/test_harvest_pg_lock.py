"""B7: Postgres advisory lock for search harvest (per login slot)."""

from __future__ import annotations

import pytest

from scraper.config import settings
from scraper.harvest_lock import HARVEST_RUN_LOCK_BASE, harvest_run_lock_key, harvest_run_pg_lock


def test_harvest_run_lock_key_per_slot():
    assert harvest_run_lock_key(1) == HARVEST_RUN_LOCK_BASE + 1
    assert harvest_run_lock_key(2) == HARVEST_RUN_LOCK_BASE + 2
    with pytest.raises(ValueError):
        harvest_run_lock_key(3)


@pytest.mark.asyncio
async def test_concurrent_harvest_lock_second_refused():
    async with harvest_run_pg_lock(1) as first:
        assert first is True
        async with harvest_run_pg_lock(1) as second:
            assert second is False


@pytest.mark.asyncio
async def test_harvest_lock_released_after_exception():
    with pytest.raises(RuntimeError, match="boom"):
        async with harvest_run_pg_lock(2) as held:
            assert held is True
            raise RuntimeError("boom")
    async with harvest_run_pg_lock(2) as again:
        assert again is True


@pytest.mark.asyncio
async def test_harvest_lock_disabled_by_flag(monkeypatch):
    monkeypatch.setattr(settings, "SSA_HARVEST_PG_LOCK", False)
    async with harvest_run_pg_lock(1) as held:
        assert held is True
