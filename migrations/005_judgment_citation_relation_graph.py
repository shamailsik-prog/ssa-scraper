"""
005 — Judgment citation relation graph persistence.

Adds an internal judgment_citation_relation table for verified citation edges
between judgments with source span evidence and reconciliation status.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    from scraper import models  # noqa: F401
    from scraper.database import Base

    table = Base.metadata.tables["judgment_citation_relation"]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[table], checkfirst=True))
    await conn.execute(
        text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_judgment_citation_relation_status') THEN "
            "ALTER TABLE judgment_citation_relation ADD CONSTRAINT ck_judgment_citation_relation_status "
            "CHECK (resolution_status IN ('linked','ambiguous','unresolved')); END IF; END $$;"
        )
    )
