"""
002 — ScrapeGraphAI integration amendment (D-15).

Forward migration for databases created before the amendment: adds the per-source extraction
policy columns (§7), the extraction_audit and scrapegraph_cache tables (§8), the AI error
column, and the usage ledger. Every statement is idempotent so re-running is harmless.
"""

from __future__ import annotations

from sqlalchemy import text

SOURCE_COLUMNS = [
    ("ai_extract_enabled", "BOOLEAN NOT NULL DEFAULT true"),
    ("extraction_mode", "VARCHAR(30) NOT NULL DEFAULT 'hybrid'"),
    ("extraction_min_confidence", "DOUBLE PRECISION NOT NULL DEFAULT 0.85"),
    ("scrapegraph_schema_version", "INTEGER NOT NULL DEFAULT 1"),
    ("crawl_allowed", "BOOLEAN NOT NULL DEFAULT false"),
    ("crawl_max_depth", "INTEGER NOT NULL DEFAULT 2"),
    ("crawl_max_pages", "INTEGER NOT NULL DEFAULT 50"),
    ("last_ai_error", "VARCHAR(2000)"),
]

STAGING_COLUMNS = [
    ("deterministic_json", "JSONB"),
    ("ai_json", "JSONB"),
    ("reconciled_json", "JSONB"),
    ("extraction_engine", "VARCHAR(40)"),
]


async def upgrade(conn) -> None:
    from scraper import models  # noqa: F401
    from scraper.database import Base

    for col, ddl in SOURCE_COLUMNS:
        await conn.execute(text(f"ALTER TABLE scraper_sources ADD COLUMN IF NOT EXISTS {col} {ddl}"))
    for tbl in ("scraper_staging", "statutes_staging"):
        for col, ddl in STAGING_COLUMNS:
            await conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {ddl}"))
    await conn.execute(text("ALTER TABLE judgment ADD COLUMN IF NOT EXISTS extraction_engine VARCHAR(40)"))
    # New tables of the amendment (no-op where 001 already created them on a fresh database).
    tables = [Base.metadata.tables[t] for t in ("extraction_audit", "scrapegraph_cache", "sgai_usage_daily", "notifications")]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables, checkfirst=True))
    await conn.execute(
        text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_source_extraction_mode') THEN "
            "ALTER TABLE scraper_sources ADD CONSTRAINT ck_source_extraction_mode CHECK "
            "(extraction_mode IN ('deterministic','hybrid','scrapegraph_managed','scrapegraph_local')); END IF; END $$;"
        )
    )
