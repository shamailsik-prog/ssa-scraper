"""B6/R1: init_db must release session advisory locks after failures (mutant RB1)."""

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


@pytest.mark.asyncio
async def test_init_db_ensure_roles_failure_preserves_error_and_releases_advisory_lock(monkeypatch):
    async def _broken_grants(lock_conn, status):
        await lock_conn.execute(text("SELECT 1/0"))
        raise RuntimeError("should not run")

    monkeypatch.setattr("scraper.database._apply_role_grants", _broken_grants)

    with pytest.raises(Exception) as exc_info:
        await init_db()
    assert "division" in str(exc_info.value).lower() or exc_info.typename in {
        "DivisionByZeroError",
        "DataError",
        "DatabaseError",
    }

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
