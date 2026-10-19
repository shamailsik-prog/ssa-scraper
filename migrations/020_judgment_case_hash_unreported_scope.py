"""
020 — Allow one unreported and one reported judgment row per (court, docket, date, text hash).

B2 keeps both rows and links them; the B1 uniqueness rule still blocks duplicate rows of the same kind.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    await conn.execute(
        text(
            "ALTER TABLE judgment ADD CONSTRAINT uq_judgment_case_text_hash "
            "UNIQUE (court_id, docket_key, decision_date, full_text_hash, unreported)"
        )
    )


async def downgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    await conn.execute(
        text(
            "ALTER TABLE judgment ADD CONSTRAINT uq_judgment_case_text_hash "
            "UNIQUE (court_id, docket_key, decision_date, full_text_hash)"
        )
    )
