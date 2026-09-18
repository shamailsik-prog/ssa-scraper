"""
004 — Instrument relation graph persistence.

Adds an internal instrument_relation table for verified cross-document relation
edges with source-document provenance and evidence snippets.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    from scraper import models  # noqa: F401
    from scraper.database import Base

    table = Base.metadata.tables["instrument_relation"]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[table], checkfirst=True))
    await conn.execute(
        text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_instrument_relation_target_present') THEN "
            "ALTER TABLE instrument_relation ADD CONSTRAINT ck_instrument_relation_target_present "
            "CHECK (target_instrument_id IS NOT NULL OR target_statute_id IS NOT NULL); END IF; END $$;"
        )
    )
