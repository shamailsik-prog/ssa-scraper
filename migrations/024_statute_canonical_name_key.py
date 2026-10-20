"""
024 — Indexed canonical_name_key for statute mention resolution (B3). Backfills from Python canonicaliser.
"""

from __future__ import annotations

from sqlalchemy import text

from scraper.parsers.citation_extractor import canonicalise_statute_name


def _canonical_key(name: str) -> str:
    canonical = canonicalise_statute_name(name) or (name or "").strip()
    return canonical.casefold()


async def upgrade(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE statute
                ADD COLUMN IF NOT EXISTS canonical_name_key VARCHAR(1000)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_statute_canonical_name_key
                ON statute (canonical_name_key)
            """
        )
    )
    rows = (await conn.execute(text("SELECT id, name FROM statute"))).fetchall()
    for row in rows:
        key = _canonical_key(row.name)
        await conn.execute(
            text("UPDATE statute SET canonical_name_key = :key WHERE id = :id"),
            {"key": key, "id": row.id},
        )


async def downgrade(conn) -> None:
    await conn.execute(text("DROP INDEX IF EXISTS ix_statute_canonical_name_key"))
    await conn.execute(text("ALTER TABLE statute DROP COLUMN IF EXISTS canonical_name_key"))
