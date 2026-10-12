"""
014 — reopen PakistanLawSite search-harvest queries that were marked done too early.

Queries with site_total_results unknown and only one page enumerated were incorrectly marked
done when pagination ended without a next link (for example unparsed totals or a transient
browser error). Re-queue them so ticks resume pagination.
"""

from __future__ import annotations

from sqlalchemy import text

REOPEN_REASON = "reopened: marked done without known site total and at most one page enumerated"


async def upgrade(conn) -> None:
    await conn.execute(
        text(
            """
            UPDATE pls_search_harvest_query
            SET status = 'in_progress',
                pages_enumerated = 0,
                rows_seen = 0,
                rows_known = 0,
                rows_new = 0,
                gap_size = 0,
                cursor_json = '{"page": 1, "row_index": 0}'::jsonb,
                last_error = :reason
            WHERE source_name = 'PakistanLawSite'
              AND status = 'done'
              AND site_total_results IS NULL
              AND COALESCE(pages_enumerated, 0) <= 1
            """
        ),
        {"reason": REOPEN_REASON},
    )
