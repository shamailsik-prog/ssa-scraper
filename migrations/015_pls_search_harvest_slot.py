"""
015 — the two PakistanLawSite logins harvest the search ledger at the same time, each on its own reporter share
(handbook R3.1; owner's decision of 13 October 2026). A ledger row records the slot whose tick last took it, and
ticks pick by priority, then by the oldest last run, so that order gets an index.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE pls_search_harvest_query ADD COLUMN IF NOT EXISTS slot SMALLINT"))
    await conn.execute(
        text(
            "COMMENT ON COLUMN pls_search_harvest_query.slot IS "
            "'login slot (1 or 2) whose harvest tick last took this row'"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_pls_search_harvest_pick "
            "ON pls_search_harvest_query (source_name, status, priority, last_run_at)"
        )
    )
