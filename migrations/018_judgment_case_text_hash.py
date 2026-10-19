"""
018 — Scope full-text hash uniqueness to court + docket + decision date.

Identical judgment text from a different case must be allowed to coexist; the hash remains a dedupe signal
only when court, docket and date agree (R1.2).
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    conflicts = await conn.execute(
        text(
            """
            SELECT court_id, docket_number, decision_date, full_text_hash, count(*) AS n
            FROM judgment
            WHERE full_text_hash IS NOT NULL
              AND court_id IS NOT NULL
              AND docket_number IS NOT NULL
              AND decision_date IS NOT NULL
            GROUP BY 1, 2, 3, 4
            HAVING count(*) > 1
            LIMIT 5
            """
        )
    )
    rows = conflicts.fetchall()
    if rows:
        raise RuntimeError(
            "cannot apply 018_judgment_case_text_hash: duplicate (court_id, docket_number, decision_date, full_text_hash) rows exist"
        )
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_full_text_hash"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_full_text_hash ON judgment (full_text_hash)"))
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    await conn.execute(
        text(
            "ALTER TABLE judgment ADD CONSTRAINT uq_judgment_case_text_hash "
            "UNIQUE (court_id, docket_number, decision_date, full_text_hash)"
        )
    )


async def downgrade(conn) -> None:
    dupes = await conn.execute(
        text(
            """
            SELECT full_text_hash, count(*) AS n
            FROM judgment
            WHERE full_text_hash IS NOT NULL
            GROUP BY 1
            HAVING count(*) > 1
            LIMIT 5
            """
        )
    )
    if dupes.fetchall():
        raise RuntimeError(
            "cannot downgrade 018_judgment_case_text_hash: duplicate full_text_hash values exist"
        )
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    await conn.execute(text("DROP INDEX IF EXISTS ix_judgment_full_text_hash"))
    await conn.execute(
        text("ALTER TABLE judgment ADD CONSTRAINT uq_judgment_full_text_hash UNIQUE (full_text_hash)")
    )
