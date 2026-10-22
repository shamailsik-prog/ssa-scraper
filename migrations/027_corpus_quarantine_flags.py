"""027 — corpus_quarantined flags for bad-document quarantine (B7)."""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(
        text(
            """
            ALTER TABLE statute
            ADD COLUMN IF NOT EXISTS corpus_quarantined BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS corpus_quarantine_reason VARCHAR(500)
            """
        )
    )
    await conn.execute(
        text(
            """
            ALTER TABLE judgment
            ADD COLUMN IF NOT EXISTS corpus_quarantined BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS corpus_quarantine_reason VARCHAR(500)
            """
        )
    )


async def downgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(
        text(
            """
            ALTER TABLE statute
            DROP COLUMN IF EXISTS corpus_quarantine_reason,
            DROP COLUMN IF EXISTS corpus_quarantined
            """
        )
    )
    await conn.execute(
        text(
            """
            ALTER TABLE judgment
            DROP COLUMN IF EXISTS corpus_quarantine_reason,
            DROP COLUMN IF EXISTS corpus_quarantined
            """
        )
    )
