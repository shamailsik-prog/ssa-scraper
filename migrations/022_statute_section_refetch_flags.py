"""
022 — Mark legacy sectionless / quality-gap statutes for re-fetch (B3). No row deletion.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE statute
                ADD COLUMN IF NOT EXISTS needs_section_refetch BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS quality_flag_no_year BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS quality_flag_duplicate_name BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
    )
    await conn.execute(
        text(
            """
            UPDATE statute s
            SET needs_section_refetch = TRUE
            WHERE NOT EXISTS (
                SELECT 1 FROM statute_section ss WHERE ss.statute_id = s.id
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            UPDATE statute
            SET quality_flag_no_year = TRUE
            WHERE year_enacted IS NULL
            """
        )
    )
    await conn.execute(
        text(
            """
            UPDATE statute s
            SET quality_flag_duplicate_name = TRUE
            FROM (
                SELECT lower(name) AS lname
                FROM statute
                GROUP BY lower(name)
                HAVING COUNT(*) > 1
            ) dup
            WHERE lower(s.name) = dup.lname
            """
        )
    )


async def downgrade(conn) -> None:
    row = (
        await conn.execute(
            text(
                """
                SELECT 1 FROM statute
                WHERE needs_section_refetch
                   OR quality_flag_no_year
                   OR quality_flag_duplicate_name
                LIMIT 1
                """
            )
        )
    ).first()
    if row is not None:
        raise RuntimeError(
            "cannot downgrade 022_statute_section_refetch_flags: statute quality/refetch flags are set"
        )
    await conn.execute(
        text(
            """
            ALTER TABLE statute
                DROP COLUMN IF EXISTS quality_flag_duplicate_name,
                DROP COLUMN IF EXISTS quality_flag_no_year,
                DROP COLUMN IF EXISTS needs_section_refetch
            """
        )
    )
