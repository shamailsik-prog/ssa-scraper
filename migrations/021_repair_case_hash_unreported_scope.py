"""
021 — Idempotent repair: ensure scoped hash uniqueness includes unreported (B2).

Some environments recorded 020 before the constraint DDL completed; this migration is safe to re-apply.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    row = await conn.execute(
        text(
            """
            SELECT pg_get_constraintdef(oid) AS def
            FROM pg_constraint
            WHERE conname = 'uq_judgment_case_text_hash'
            """
        )
    )
    defn = (row.scalar() or "").lower()
    if "unreported" in defn:
        return
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    await conn.execute(
        text(
            "ALTER TABLE judgment ADD CONSTRAINT uq_judgment_case_text_hash "
            "UNIQUE (court_id, docket_key, decision_date, full_text_hash, unreported)"
        )
    )


async def downgrade(conn) -> None:
    row = await conn.execute(
        text(
            """
            SELECT pg_get_constraintdef(oid) AS def
            FROM pg_constraint
            WHERE conname = 'uq_judgment_case_text_hash'
            """
        )
    )
    defn = (row.scalar() or "").lower()
    if "unreported" not in defn:
        return
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    await conn.execute(
        text(
            "ALTER TABLE judgment ADD CONSTRAINT uq_judgment_case_text_hash "
            "UNIQUE (court_id, docket_key, decision_date, full_text_hash)"
        )
    )
