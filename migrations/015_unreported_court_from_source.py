"""
015 — The court of an unreported judgment is the court whose website published it.

Unreported judgments promoted before this read their court from the text, so a Sindh High Court order
that discusses a Supreme Court precedent was filed under the Supreme Court. Re-file them under the
source's court (scraper.unreported.SOURCE_COURT_CODE). Only judgments marked unreported are touched.
"""

from __future__ import annotations

from sqlalchemy import text

from scraper.unreported import SOURCE_COURT_CODE


async def upgrade(conn) -> None:
    for source_name, code in SOURCE_COURT_CODE.items():
        await conn.execute(
            text(
                "UPDATE judgment j SET court_id = c.id, court_name = c.name FROM court c "
                "WHERE j.unreported AND j.source_name = :source AND c.short_code = :code "
                "AND (j.court_id IS DISTINCT FROM c.id OR j.court_name IS DISTINCT FROM c.name)"
            ),
            {"source": source_name, "code": code},
        )
