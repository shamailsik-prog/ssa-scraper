"""
024 — Indexed canonical_name_key for statute mention resolution (B3).

Adds the column in the migration transaction, backfills in batches (only NULL keys), then builds
the index CONCURRENTLY after commit (see upgrade_post_commit + run_migrations).
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
    await _backfill_canonical_name_keys(conn)


async def upgrade_post_commit() -> None:
    from scraper.database import engine

    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            text(
                """
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_statute_canonical_name_key
                    ON statute (canonical_name_key)
                """
            )
        )


async def downgrade(conn) -> None:
    await conn.execute(text("DROP INDEX IF EXISTS ix_statute_canonical_name_key"))
    await conn.execute(text("ALTER TABLE statute DROP COLUMN IF EXISTS canonical_name_key"))
