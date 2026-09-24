"""
009 — spot_check: re-fetched records compared with the corpus (operator request, 24 September 2026).
"""

from __future__ import annotations


async def upgrade(conn) -> None:
    from scraper import models
    from scraper.database import Base

    table = Base.metadata.tables["spot_check"]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[table], checkfirst=True))
