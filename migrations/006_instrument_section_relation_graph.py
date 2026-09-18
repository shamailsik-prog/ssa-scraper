"""
006 — Instrument section amendment relation graph persistence.

Adds an internal instrument_section_relation table for verified statute-section
amendment-operation edges derived from instrument text.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    from scraper import models  # noqa: F401
    from scraper.database import Base

    table = Base.metadata.tables["instrument_section_relation"]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[table], checkfirst=True))
    await conn.execute(
        text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_instrument_section_relation_operation') THEN "
            "ALTER TABLE instrument_section_relation ADD CONSTRAINT ck_instrument_section_relation_operation "
            "CHECK (amendment_operation IN ('insert','substitute','omit','repeal')); END IF; END $$;"
        )
    )
