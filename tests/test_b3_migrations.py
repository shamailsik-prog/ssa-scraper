"""B3 migration 022/023 guards."""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import select, text

from scraper.database import engine
from scraper.models import Statute


@pytest.mark.asyncio
async def test_migration_023_creates_lower_name_index(db):
    mig = importlib.import_module("migrations.023_statute_name_lower_index")
    async with engine.begin() as conn:
        await mig.upgrade(conn)
        row = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_statute_name_lower' LIMIT 1"
                )
            )
        ).first()
        indexdef = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_statute_name_lower' LIMIT 1"
                )
            )
        ).scalar()
        assert row is not None
        assert indexdef is not None
        assert "lower(" in indexdef.lower() and "name" in indexdef.lower()
        await mig.downgrade(conn)


@pytest.mark.asyncio
async def test_migration_024_backfills_canonical_name_key(db):
    mig = importlib.import_module("migrations.024_statute_canonical_name_key")
    from scraper.tasks.promotion import _normalized_statute_name_key

    db.add(Statute(name="Court fees Act, 1870", jurisdiction="Federal", year_enacted=1870))
    await db.commit()
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    row = (
        await db.execute(select(Statute).where(Statute.name == "Court fees Act, 1870"))
    ).scalars().one()
    assert row.canonical_name_key == _normalized_statute_name_key("Court fees Act, 1870")


@pytest.mark.asyncio
async def test_migration_022_downgrade_refuses_when_refetch_flags_set(db):
    mig = importlib.import_module("migrations.022_statute_section_refetch_flags")
    db.add(Statute(name="Flagged Act", jurisdiction="Federal", needs_section_refetch=True))
    await db.commit()
    async with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="quality/refetch flags are set"):
            await mig.downgrade(conn)
