"""025 — Persist Urdu headnote mojibake flag on judgments (B4 #235)."""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE judgment
            ADD COLUMN IF NOT EXISTS quality_flag_urdu_headnote_mojibake BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
    )


async def downgrade(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE judgment
            DROP COLUMN IF EXISTS quality_flag_urdu_headnote_mojibake
            """
        )
    )
