"""Reader role must use column-level SELECT on content tables (grants ops)."""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text

from scraper.config import settings
from scraper.database import engine, ensure_roles
from scraper.reader_grants import (
    READER_COLUMN_LIMITED_TABLES,
    READER_OPS_COLUMN_ALLOWLIST,
    assert_no_table_wide_reader_grant,
    is_reader_blocked_column,
    ops_reapply_grant_snippet,
    reader_select_columns,
)
from scraper.models import CONTRACT_TABLES


def _dsn(user: str, pw: str) -> str:
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "")
    hostpart = url.split("@", 1)[1]
    return f"postgresql://{user}:{pw}@{hostpart}"


@pytest.mark.parametrize(
    "col",
    [
        "raw_html",
        "full_text",
        "headnotes",
        "headnote_stripped_full_text",
        "embedding",
        "ai_json",
        "route_json",
    ],
)
def test_blocked_column_names(col: str):
    assert is_reader_blocked_column(col)


def test_ops_snippets_reject_table_wide_grant():
    bad = "GRANT SELECT ON public.scraper_staging TO sikander_reader;"
    with pytest.raises(ValueError, match="table-wide"):
        assert_no_table_wide_reader_grant(bad, table_name="scraper_staging")
    good = ops_reapply_grant_snippet("scraper_staging")
    assert "GRANT SELECT (" in good and "raw_html" not in good


async def test_ops_allowlist_matches_live_schema(db):
    await ensure_roles()
    async with engine.connect() as conn:
        for table in READER_COLUMN_LIMITED_TABLES:
            if table not in CONTRACT_TABLES and table not in ("scraper_staging", "statutes_staging"):
                continue
            rows = (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position"
                    ),
                    {"t": table},
                )
            ).fetchall()
            live = [r[0] for r in rows]
            expected = reader_select_columns(table, live)
            assert expected == READER_OPS_COLUMN_ALLOWLIST[table]


async def test_reader_cannot_select_content_columns_on_judgment(db):
    await ensure_roles()
    conn = await asyncpg.connect(_dsn(settings.SIKANDER_READER_ROLE, settings.SIKANDER_READER_PASSWORD.get_secret_value()))
    try:
        await conn.fetch('SELECT id, canonical_citation FROM judgment LIMIT 1')
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT full_text FROM judgment LIMIT 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM judgment LIMIT 1")
    finally:
        await conn.close()


async def test_reader_contract_tables_use_safe_select(db):
    await ensure_roles()
    conn = await asyncpg.connect(_dsn(settings.SIKANDER_READER_ROLE, settings.SIKANDER_READER_PASSWORD.get_secret_value()))
    try:
        for t in CONTRACT_TABLES:
            if t in READER_COLUMN_LIMITED_TABLES:
                cols = ", ".join(f'"{c}"' for c in READER_OPS_COLUMN_ALLOWLIST[t])
                await conn.fetch(f'SELECT {cols} FROM "{t}" LIMIT 1')
            else:
                await conn.fetch(f'SELECT * FROM "{t}" LIMIT 1')
    finally:
        await conn.close()
