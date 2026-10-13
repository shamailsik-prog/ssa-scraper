"""
014 — Unreported judgments.

Judgments from a court's own website that no law report cites enter the corpus marked `unreported`,
with the kind of proceeding their case number names (scraper/unreported.py).
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE judgment ADD COLUMN IF NOT EXISTS unreported BOOLEAN NOT NULL DEFAULT FALSE"))
    await conn.execute(text("ALTER TABLE judgment ADD COLUMN IF NOT EXISTS proceeding_type VARCHAR(40)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_unreported_proceeding ON judgment (unreported, proceeding_type)"))
