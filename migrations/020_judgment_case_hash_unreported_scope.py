"""
020 — Allow one unreported and one reported judgment row per (court, docket, date, text hash).

B2 keeps both rows and links them; the B1 uniqueness rule still blocks duplicate rows of the same kind.
"""

from __future__ import annotations

from sqlalchemy import text

_CASE_KEY_SQL = """
SELECT court_id, docket_key, decision_date, full_text_hash, count(*) AS n
FROM judgment
WHERE full_text_hash IS NOT NULL
  AND court_id IS NOT NULL
  AND docket_key IS NOT NULL
  AND decision_date IS NOT NULL
GROUP BY 1, 2, 3, 4
HAVING count(*) > 1
LIMIT 5
"""


async def upgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    await conn.execute(
        text(
            "ALTER TABLE judgment ADD CONSTRAINT uq_judgment_case_text_hash "
            "UNIQUE (court_id, docket_key, decision_date, full_text_hash, unreported)"
        )
    )


async def downgrade(conn) -> None:
    conflicts = await conn.execute(text(_CASE_KEY_SQL))
    if conflicts.fetchall():
        raise RuntimeError(
            "cannot downgrade 020_judgment_case_hash_unreported_scope: "
            "duplicate (court_id, docket_key, decision_date, full_text_hash) rows exist"
        )
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    await conn.execute(
        text(
            "ALTER TABLE judgment ADD CONSTRAINT uq_judgment_case_text_hash "
            "UNIQUE (court_id, docket_key, decision_date, full_text_hash)"
        )
    )
