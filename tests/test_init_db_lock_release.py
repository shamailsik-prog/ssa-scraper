"""B6: init_db must release session advisory locks after DDL failure (mutant RB1)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from scraper.database import (
    PG_ADVISORY_INIT_DB_KEY1,
    PG_ADVISORY_INIT_DB_KEY2,
    engine,
    init_db,
)


class _InitDbMigrationBoom(Exception):
    pass


@pytest.mark.asyncio
async def test_init_db_ddl_failure_preserves_error_and_releases_advisory_lock(monkeypatch):
    async def _boom():
        raise _InitDbMigrationBoom("simulated migration failure")

    monkeypatch.setattr("scraper.database.run_migrations", _boom)

    with pytest.raises(_InitDbMigrationBoom, match="simulated migration failure"):
        await init_db()

    async with engine.connect() as conn:
        held = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND classid = :k1 AND objid = :k2 AND granted"
                ),
                {"k1": PG_ADVISORY_INIT_DB_KEY1, "k2": PG_ADVISORY_INIT_DB_KEY2},
            )
        ).scalar()
    assert held == 0
