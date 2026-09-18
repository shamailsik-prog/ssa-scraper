"""
003 — Instrument mention storage for Gazette citation/statute linking.

Adds JSONB columns on instrument rows to retain extracted mention provenance and
canonical statute links derived during promotion.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE instrument ADD COLUMN IF NOT EXISTS citation_mentions JSONB"))
    await conn.execute(text("ALTER TABLE instrument ADD COLUMN IF NOT EXISTS statute_mentions JSONB"))
