"""Reader role must use column-level SELECT on content tables (grants ops)."""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text

from scraper.config import settings
from scraper.database import engine, ensure_roles
from scraper.reader_grants import (
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


def test_any_column_name_containing_embedding_is_blocked():
    assert is_reader_blocked_column("embedding")
    assert is_reader_blocked_column("embedding_model")


def test_ops_snippets_reject_table_wide_grant():
    bad = "GRANT SELECT ON public.scraper_staging TO sikander_reader;"
    with pytest.raises(ValueError, match="table-wide"):
        assert_no_table_wide_reader_grant(bad, table_name="scraper_staging")
    good = ops_reapply_grant_snippet("scraper_staging")
    assert "GRANT SELECT (" in good and "raw_html" not in good


async def test_ops_allowlist_matches_live_schema(db):
    await ensure_roles()
    async with engine.connect() as conn:
        for table, frozen in READER_OPS_COLUMN_ALLOWLIST.items():
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
            assert expected == frozen


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


async def test_reader_cannot_select_statute_section_embedding(db):
    await ensure_roles()
    conn = await asyncpg.connect(_dsn(settings.SIKANDER_READER_ROLE, settings.SIKANDER_READER_PASSWORD.get_secret_value()))
    try:
        await conn.fetch("SELECT id, section_number FROM statute_section LIMIT 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT embedding FROM statute_section LIMIT 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM statute_section LIMIT 1")
    finally:
        await conn.close()


async def test_reader_keeps_table_wide_on_statute_version_and_treatment(db):
    await ensure_roles()
    conn = await asyncpg.connect(_dsn(settings.SIKANDER_READER_ROLE, settings.SIKANDER_READER_PASSWORD.get_secret_value()))
    try:
        await conn.fetch("SELECT * FROM statute_section_version LIMIT 1")
        await conn.fetch("SELECT evidence_passage FROM treatment LIMIT 1")
        await conn.fetch("SELECT * FROM treatment LIMIT 1")
    finally:
        await conn.close()


async def test_reader_contract_tables_use_safe_select(db):
    await ensure_roles()
    conn = await asyncpg.connect(_dsn(settings.SIKANDER_READER_ROLE, settings.SIKANDER_READER_PASSWORD.get_secret_value()))
    try:
        for t in CONTRACT_TABLES:
            if t in READER_OPS_COLUMN_ALLOWLIST:
                cols = ", ".join(f'"{c}"' for c in READER_OPS_COLUMN_ALLOWLIST[t])
                await conn.fetch(f'SELECT {cols} FROM "{t}" LIMIT 1')
            else:
                await conn.fetch(f'SELECT * FROM "{t}" LIMIT 1')
    finally:
        await conn.close()
