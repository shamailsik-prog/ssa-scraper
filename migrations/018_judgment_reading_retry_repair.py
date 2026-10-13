"""
018 — Forward-only repair for reader reliability schema additions that were added after migration 017
had already been applied on some databases.

Never rely on editing an applied migration. This migration is intentionally idempotent so both fresh
databases (where 017 already contains these objects) and upgraded databases converge on the same schema.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(
        text("ALTER TABLE judgment_reading_version ADD COLUMN IF NOT EXISTS retry_after_at TIMESTAMPTZ")
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_judgment_reading_version_attempt_idx "
            "ON judgment_reading_version (judgment_id, attempt_no)"
        )
    )
