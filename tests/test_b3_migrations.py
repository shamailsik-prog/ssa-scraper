"""B3 migration 022/023/024 guards."""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from scraper.database import engine, run_migrations
from scraper.models import QuarantineQueue, Statute
from scraper.tasks.promotion import _normalized_statute_name_key, promote_statute_staging
from tests.test_b3_statute_promotion import _make_statute_staging

_MIGRATION_024 = "024_statute_canonical_name_key"


async def _drop_migration_024_schema() -> None:
    """Test-only teardown: non-concurrent drops avoid blocking behind open test sessions."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX IF EXISTS ix_statute_canonical_name_key"))
        await conn.execute(text("ALTER TABLE statute DROP COLUMN IF EXISTS canonical_name_key"))


async def _delete_migration_024_version() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM schema_migrations WHERE version = :v"), {"v": _MIGRATION_024})


async def _canonical_index_row(conn):
    return (
        await conn.execute(
            text(
                """
                SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) AS indexdef
                FROM pg_class c
                JOIN pg_index i ON i.indexrelid = c.oid
                JOIN pg_class t ON t.oid = i.indrelid
                WHERE c.relname = 'ix_statute_canonical_name_key'
                  AND t.relname = 'statute'
                LIMIT 1
                """
            )
        )
    ).first()


@pytest.mark.asyncio
async def test_migration_023_creates_lower_name_index(db):
    mig = importlib.import_module("migrations.023_statute_name_lower_index")
    async with engine.begin() as conn:
        await mig.upgrade(conn)
        row = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_statute_name_lower' LIMIT 1"
                )
            )
        ).first()
        indexdef = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_statute_name_lower' LIMIT 1"
                )
            )
        ).scalar()
        assert row is not None
        assert indexdef is not None
        assert "lower(" in indexdef.lower() and "name" in indexdef.lower()
        await mig.downgrade(conn)


@pytest.mark.asyncio
async def test_migration_024_backfills_canonical_name_key(db):
    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
    from scraper.tasks.promotion import _normalized_statute_name_key

    await _drop_migration_024_schema()

    irregular = "  The   Court  fees  Act ,  1870.  "
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO statute (name, jurisdiction, year_enacted) VALUES (:n, 'Federal', 1870)"
            ),
            {"n": irregular},
        )
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    async with engine.connect() as conn:
        key_before_post = (
            await conn.execute(
                text("SELECT canonical_name_key FROM statute WHERE name = :n"),
                {"n": irregular},
            )
        ).scalar()
    assert key_before_post is None
    await db.close()
    await mig.upgrade_post_commit()

    async with engine.connect() as conn:
        idx = await _canonical_index_row(conn)
    assert idx is not None
    assert idx.indisvalid is True
    assert idx.indexdef is not None
    assert "canonical_name_key" in idx.indexdef.lower()

    async with engine.connect() as conn:
        key = (
            await conn.execute(
                text("SELECT canonical_name_key FROM statute WHERE name = :n"),
                {"n": irregular},
            )
        ).scalar()
    assert key == _normalized_statute_name_key(irregular)


def test_migration_024_backfill_update_keeps_null_guard_in_sql():
    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
    import inspect

    source = inspect.getsource(mig._backfill_canonical_name_keys)
    assert "WHERE s.id = v.id AND s.canonical_name_key IS NULL" in source


@pytest.mark.asyncio
async def test_migration_024_backfill_preserves_preset_canonical_name_key(db):
    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
    await _drop_migration_024_schema()

    preset = "preset-key-not-from-name"
    name = "Preset Key Act, 1900"
    assert preset != _normalized_statute_name_key(name)
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    db.add(
        Statute(
            name=name,
            jurisdiction="Federal",
            year_enacted=1900,
        )
    )
    await db.commit()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE statute SET canonical_name_key = :k WHERE name = :n"),
            {"k": preset, "n": name},
        )
    await db.close()
    await mig.upgrade_post_commit()

    async with engine.connect() as conn:
        preset_after = (
            await conn.execute(
                text("SELECT canonical_name_key FROM statute WHERE name = :n"),
                {"n": name},
            )
        ).scalar()
    assert preset_after == preset


@pytest.mark.asyncio
async def test_run_migrations_applies_024_when_version_row_missing(db):
    await _drop_migration_024_schema()
    await _delete_migration_024_version()

    irregular = "Run Migrations Backfill Act , 1911"
    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    db.add(Statute(name=irregular, jurisdiction="Federal", year_enacted=1911))
    await db.commit()
    await db.close()

    applied = await run_migrations()
    assert _MIGRATION_024 in applied

    async with engine.connect() as conn:
        recorded = (
            await conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :v"),
                {"v": _MIGRATION_024},
            )
        ).first()
        idx = await _canonical_index_row(conn)
    assert recorded is not None
    assert idx is not None and idx.indisvalid is True

    async with engine.connect() as conn:
        key = (
            await conn.execute(
                text("SELECT canonical_name_key FROM statute WHERE name = :n"),
                {"n": irregular},
            )
        ).scalar()
    assert key == _normalized_statute_name_key(irregular)


@pytest.mark.asyncio
async def test_run_migrations_does_not_record_024_when_post_commit_hook_fails(monkeypatch):
    await _drop_migration_024_schema()
    async with engine.begin() as conn:
        mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
        await mig.upgrade(conn)
    await _delete_migration_024_version()
    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")

    async def _boom() -> None:
        raise RuntimeError("post_commit hook failed for test")

    monkeypatch.setattr(mig, "upgrade_post_commit", _boom)

    with pytest.raises(RuntimeError, match="post_commit hook failed"):
        await run_migrations()

    async with engine.connect() as conn:
        recorded = (
            await conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :v"),
                {"v": _MIGRATION_024},
            )
        ).first()
    assert recorded is None

    monkeypatch.undo()
    await mig.upgrade_post_commit()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO schema_migrations(version) VALUES (:v) ON CONFLICT DO NOTHING"),
            {"v": _MIGRATION_024},
        )


@pytest.mark.asyncio
async def test_run_migrations_rebuilds_invalid_canonical_name_key_index(db):
    from scraper.database import ensure_statute_canonical_name_key_index_valid

    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
    await _drop_migration_024_schema()
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    await db.close()
    await mig.upgrade_post_commit()

    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        await conn.execute(
            text(
                """
                UPDATE pg_index AS i
                SET indisvalid = false
                FROM pg_class c
                WHERE i.indexrelid = c.oid
                  AND c.relname = 'ix_statute_canonical_name_key'
                """
            )
        )
        before = await _canonical_index_row(conn)
    assert before is not None and before.indisvalid is False

    await ensure_statute_canonical_name_key_index_valid()

    async with engine.connect() as conn:
        after = await _canonical_index_row(conn)
    assert after is not None
    assert after.indisvalid is True
    assert "canonical_name_key" in after.indexdef.lower()


@pytest.mark.asyncio
async def test_year_after_current_calendar_year_quarantines_statute_no_year(db):
    next_year = datetime.now(timezone.utc).year + 1
    st = await _make_statute_staging(
        db,
        {
            "statute_name": f"Next Calendar Year Act, {next_year}",
            "year_enacted": next_year,
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_title": "Short title",
                    "section_text": "1. This Act is one year beyond the promotion ceiling.",
                }
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    row = (
        await db.execute(select(QuarantineQueue).where(QuarantineQueue.statutes_staging_id == st.id))
    ).scalars().one()
    assert (row.details or {}).get("reason_code") == "statute_no_year"


@pytest.mark.asyncio
async def test_migration_022_downgrade_refuses_when_refetch_flags_set(db):
    mig = importlib.import_module("migrations.022_statute_section_refetch_flags")
    db.add(Statute(name="Flagged Act", jurisdiction="Federal", needs_section_refetch=True))
    await db.commit()
    async with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="quality/refetch flags are set"):
            await mig.downgrade(conn)
