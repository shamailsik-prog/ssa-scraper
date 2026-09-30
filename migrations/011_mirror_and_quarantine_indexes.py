"""
011 — indexes for the 30 September 2026 durable fixes: the grid's staged-citation lookup and quarantine
supersede (scraper_staging by source + citation), review-queue lookups by staging row, and the PLS
"last judgment" / per-source counts on /status (judgment by source + promoted_at).
Plain CREATE INDEX IF NOT EXISTS: the tables hold tens of thousands of rows, a build takes seconds.
"""

from __future__ import annotations

from sqlalchemy import text

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_staging_source_citation ON scraper_staging (source_name, extracted_citation)",
    "CREATE INDEX IF NOT EXISTS ix_quarantine_staging ON quarantine_queue (staging_id)",
    "CREATE INDEX IF NOT EXISTS ix_judgment_source_promoted ON judgment (source_name, promoted_at)",
)


async def upgrade(conn) -> None:
    for ddl in INDEXES:
        await conn.execute(text(ddl))
