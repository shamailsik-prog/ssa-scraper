import pytest
from sqlalchemy import func, select

from scraper.database import seed_data
from scraper.models import Court


@pytest.mark.asyncio
async def test_seed_data_twice_does_not_raise_uq_court_name(db):
    await seed_data()
    await seed_data()
    n = (await db.execute(select(func.count()).select_from(Court))).scalar()
    assert int(n or 0) >= 1
