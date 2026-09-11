"""
001 — Annex A contract tables and the service's internal tables.

Creates every table declared in scraper.models on a fresh database. Idempotent: uses
CREATE TABLE IF NOT EXISTS semantics via SQLAlchemy's checkfirst, so a database that already
carries a table is left untouched (column additions belong to later migrations).
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    from scraper import models  # noqa: F401  (registers every table on Base.metadata)
    from scraper.database import Base

    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=True))
    # Retired pre-contract tables (Annex B-3, B-4). Dropped only if they exist from the blackletter build.
    for legacy in ("limitation_periods", "case_law_master", "statutes_master"):
        await conn.execute(text(f"DROP TABLE IF EXISTS {legacy} CASCADE"))
