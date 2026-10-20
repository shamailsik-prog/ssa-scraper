"""
023 — Functional index on lower(statute.name) for mention resolution (B3). No data changes.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_statute_name_lower
                ON statute (lower(name))
            """
        )
    )


async def downgrade(conn) -> None:
    await conn.execute(text("DROP INDEX IF EXISTS ix_statute_name_lower"))
