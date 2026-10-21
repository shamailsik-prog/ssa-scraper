"""Finding #238: court seed must survive concurrent or repeated seed_data() without uq_court_* violations."""

from __future__ import annotations

import pytest

from scraper.database import seed_data


@pytest.mark.asyncio
async def test_seed_data_twice_does_not_raise_uq_court(clean_db):
    await seed_data()
    await seed_data()
