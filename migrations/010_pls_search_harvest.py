"""
010 — PakistanLawSite search-driven gap harvest plan and per-query progress (resumable).
"""

from __future__ import annotations


async def upgrade(conn) -> None:
    from scraper import models
    from scraper.database import Base

    table = Base.metadata.tables["pls_search_harvest_query"]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[table], checkfirst=True))
