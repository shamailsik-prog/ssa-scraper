"""
019 — Forward-only repair for reader reliability schema additions that were added after migration 018
had already been applied on some databases.

Never rely on editing an applied migration. This migration is intentionally idempotent so both fresh
databases (where 018 already contains these objects) and upgraded databases converge on the same schema.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(
        text("ALTER TABLE judgment_reading_version ADD COLUMN IF NOT EXISTS retry_after_at TIMESTAMPTZ")
    )
    # Fresh databases already have the uq_judgment_reading_version_attempt constraint from 018.
    # Older databases may have applied the earlier 018 without it. Avoid creating a duplicate unique
    # index on fresh installs while repairing only the missing case.
    existing_unique = (
        await conn.execute(
            text(
                "SELECT to_regclass('public.uq_judgment_reading_version_attempt') IS NOT NULL "
                "OR to_regclass('public.uq_judgment_reading_version_attempt_idx') IS NOT NULL"
            )
        )
    ).scalar()
    if not existing_unique:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_judgment_reading_version_attempt_idx "
                "ON judgment_reading_version (judgment_id, attempt_no)"
            )
        )

