"""026 — Display-layer stripped headnote text (preserved full_text unchanged)."""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(
        text(
            """
            ALTER TABLE judgment
            ADD COLUMN IF NOT EXISTS headnote_stripped_full_text TEXT
            """
        )
    )


async def downgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(
        text(
            """
            ALTER TABLE judgment
            DROP COLUMN IF EXISTS headnote_stripped_full_text
            """
        )
    )
