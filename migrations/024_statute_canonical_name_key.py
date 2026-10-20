"""
024 — Indexed canonical_name_key for statute mention resolution (B3).

Adds the column in the migration transaction only. Batched backfill and CONCURRENTLY index build
run in upgrade_post_commit (autocommit batches + run_migrations records the version after the hook).
"""

from __future__ import annotations

from sqlalchemy import text

from scraper.parsers.citation_extractor import canonicalise_statute_name

_BATCH_SIZE = 500


def _canonical_key(name: str) -> str:
    canonical = canonicalise_statute_name(name) or (name or "").strip()
    return canonical.casefold()


async def _backfill_canonical_name_keys(conn) -> None:
    while True:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT id, name
                    FROM statute
                    WHERE canonical_name_key IS NULL
                    LIMIT :limit
                    """
                ),
                {"limit": _BATCH_SIZE},
            )
        ).fetchall()
        if not rows:
            return
        values_parts: list[str] = []
        params: dict = {}
        for i, row in enumerate(rows):
            values_parts.append(f"(CAST(:id{i} AS uuid), :key{i})")
            params[f"id{i}"] = row.id
            params[f"key{i}"] = _canonical_key(row.name)
        values_sql = ", ".join(values_parts)
        await conn.execute(
            text(
                f"""
                UPDATE statute AS s
                SET canonical_name_key = v.key
                FROM (VALUES {values_sql}) AS v(id, key)
                WHERE s.id = v.id AND s.canonical_name_key IS NULL
                """
            ),
            params,
        )


async def upgrade(conn) -> None:
    await conn.execute(
        text(
            """
            ALTER TABLE statute
                ADD COLUMN IF NOT EXISTS canonical_name_key VARCHAR(1000)
            """
        )
    )


async def upgrade_post_commit() -> None:
    from scraper.database import engine, ensure_statute_canonical_name_key_index_valid

    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        await _backfill_canonical_name_keys(conn)
    await ensure_statute_canonical_name_key_index_valid()


async def downgrade(conn) -> None:
    await conn.execute(text("DROP INDEX IF EXISTS ix_statute_canonical_name_key"))
    await conn.execute(text("ALTER TABLE statute DROP COLUMN IF EXISTS canonical_name_key"))
