"""Shared helpers for tests that briefly revoke CONNECT on database postgres."""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Distinct from production role-setup / test DB reset locks.
POSTGRES_CATALOG_PROBE_CLUSTER_LOCK = 738_238_003

_SUBPROCESS_TIMEOUT_SEC = 180


def subprocess_timeout_sec() -> int:
    return _SUBPROCESS_TIMEOUT_SEC


@asynccontextmanager
async def postgres_catalog_probe_cluster_lock(admin_url: str):
    """Serialize postgres PUBLIC CONNECT revoke across xdist workers."""
    lock_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    probe_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with lock_engine.connect() as conn:
            await conn.execute(
                text("SELECT pg_advisory_lock(:k)"),
                {"k": POSTGRES_CATALOG_PROBE_CLUSTER_LOCK},
            )
            async with probe_engine.connect() as probe_conn:
                contested = (
                    await probe_conn.execute(
                        text("SELECT pg_try_advisory_lock(:k)"),
                        {"k": POSTGRES_CATALOG_PROBE_CLUSTER_LOCK},
                    )
                ).scalar()
                assert contested is False, "postgres catalog probe cluster lock must be held"
            try:
                yield
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": POSTGRES_CATALOG_PROBE_CLUSTER_LOCK},
                )
    finally:
        await probe_engine.dispose()
        await lock_engine.dispose()


async def grant_public_connect_on_postgres(admin_url: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("GRANT CONNECT ON DATABASE postgres TO PUBLIC"))
    finally:
        await engine.dispose()


async def drop_owned_role(conn, role_name: str) -> None:
    safe = role_name.replace('"', "")
    exists = (
        await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": safe})
    ).scalar()
    if not exists:
        return
    owner = (await conn.execute(text("SELECT current_user"))).scalar()
    await conn.execute(text(f'REASSIGN OWNED BY "{safe}" TO "{owner}"'))
    await conn.execute(text(f'DROP OWNED BY "{safe}" CASCADE'))
    await conn.execute(text(f'DROP ROLE IF EXISTS "{safe}"'))
