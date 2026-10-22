"""B7: migration 027 on prod-shaped schema (no corpus_quarantined on statute/judgment)."""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import text

from scraper.database import engine


async def _column_exists(conn, table: str, column: str) -> bool:
    row = (
        await conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :t
                  AND column_name = :c
                LIMIT 1
                """
            ),
            {"t": table, "c": column},
        )
    ).first()
    return row is not None


async def _table_exists(conn, table: str) -> bool:
    row = (
        await conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = :t
                LIMIT 1
                """
            ),
            {"t": table},
        )
    ).first()
    return row is not None


async def _drop_legacy_corpus_quarantine_columns(conn) -> None:
    """Test-only: prod has no corpus_quarantined on statute/judgment."""
    for table in ("statute", "judgment"):
        if await _column_exists(conn, table, "corpus_quarantine_reason"):
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS corpus_quarantine_reason"))
        if await _column_exists(conn, table, "corpus_quarantined"):
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS corpus_quarantined"))


@pytest.mark.asyncio
async def test_migration_027_document_quarantine_on_prod_shaped_schema():
    mig = importlib.import_module("migrations.027_document_quarantine")
    async with engine.begin() as conn:
        await _drop_legacy_corpus_quarantine_columns(conn)
        assert not await _column_exists(conn, "statute", "corpus_quarantined")
        assert not await _column_exists(conn, "judgment", "corpus_quarantined")

        had_table = await _table_exists(conn, "document_quarantine")
        if had_table:
            await mig.downgrade(conn)

        await mig.upgrade(conn)
        assert await _table_exists(conn, "document_quarantine")
        pk = (
            await conn.execute(
                text(
                    """
                    SELECT a.attname
                    FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                    JOIN pg_class c ON c.oid = i.indrelid
                    WHERE c.relname = 'document_quarantine'
                      AND i.indisprimary
                    ORDER BY a.attnum
                    """
                )
            )
        ).scalars().all()
        assert pk == ["doc_type", "doc_id"]
        pk_name = (
            await conn.execute(
                text(
                    """
                    SELECT c.conname
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    WHERE t.relname = 'document_quarantine' AND c.contype = 'p'
                    LIMIT 1
                    """
                )
            )
        ).scalar()
        assert pk_name == "pk_document_quarantine"

        await mig.downgrade(conn)
        assert not await _table_exists(conn, "document_quarantine")

        if had_table:
            await mig.upgrade(conn)
