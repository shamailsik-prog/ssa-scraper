"""B3 migration 022/023/024 guards."""

from __future__ import annotations

import asyncio
import importlib
import types
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

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


def _assert_index_on_canonical_name_key(indexdef: str | None) -> None:
    assert indexdef is not None
    assert "(canonical_name_key)" in indexdef


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
    _assert_index_on_canonical_name_key(idx.indexdef)

    async with engine.connect() as conn:
        key = (
            await conn.execute(
                text("SELECT canonical_name_key FROM statute WHERE name = :n"),
                {"n": irregular},
            )
        ).scalar()
    assert key == _normalized_statute_name_key(irregular)


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
    _assert_index_on_canonical_name_key(idx.indexdef)

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
    _assert_index_on_canonical_name_key(after.indexdef)


@pytest.mark.asyncio
async def test_run_migrations_rebuilds_invalid_index_when_024_already_recorded(db):
    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
    await _drop_migration_024_schema()
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    await db.close()
    await mig.upgrade_post_commit()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO schema_migrations(version) VALUES (:v) ON CONFLICT DO NOTHING"),
            {"v": _MIGRATION_024},
        )

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

    await run_migrations()

    async with engine.connect() as conn:
        recorded = (
            await conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :v"),
                {"v": _MIGRATION_024},
            )
        ).first()
        after = await _canonical_index_row(conn)
    assert recorded is not None
    assert after is not None and after.indisvalid is True
    _assert_index_on_canonical_name_key(after.indexdef)


_PROBE_VERSION = "987_b3_hookless_migration_tx_probe"
_PROBE_TABLE = "_b3_hookless_migration_tx_probe"


@pytest.mark.asyncio
async def test_run_migrations_no_hook_migration_rolls_back_when_version_insert_fails(monkeypatch):
    async def upgrade(conn) -> None:
        await conn.execute(
            text(f"CREATE TABLE {_PROBE_TABLE} (id int PRIMARY KEY)")
        )

    fake_module = types.SimpleNamespace(upgrade=upgrade)
    real_import_module = importlib.import_module

    def import_module(name: str):
        if name == f"migrations.{_PROBE_VERSION}":
            return fake_module
        return real_import_module(name)

    monkeypatch.setattr(importlib, "import_module", import_module)
    monkeypatch.setattr("scraper.database.discover_migrations", lambda: [_PROBE_VERSION])

    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {_PROBE_TABLE}"))
        await conn.execute(
            text("DELETE FROM schema_migrations WHERE version = :v"),
            {"v": _PROBE_VERSION},
        )

    original_execute = AsyncConnection.execute

    async def execute_fail_version_insert(self, statement, *args, **kwargs):
        if "INSERT INTO schema_migrations" in str(statement):
            raise RuntimeError("simulated schema_migrations insert failure")
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", execute_fail_version_insert)

    with pytest.raises(RuntimeError, match="simulated schema_migrations insert failure"):
        await run_migrations()

    async with engine.connect() as conn:
        probe = (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": _PROBE_TABLE},
            )
        ).first()
    assert probe is None


@pytest.mark.asyncio
async def test_migration_024_backfill_commits_per_batch_on_autocommit_connection(monkeypatch, db):
    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
    monkeypatch.setattr(mig, "_BATCH_SIZE", 2)
    await _drop_migration_024_schema()
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    for i in range(4):
        db.add(
            Statute(
                name=f"Batch Commit Act {i}, 1920",
                jurisdiction="Federal",
                year_enacted=1920,
            )
        )
    await db.commit()
    await db.close()

    async def _index_only_after_backfill() -> None:
        return None

    monkeypatch.setattr(
        "scraper.database.ensure_statute_canonical_name_key_index_valid",
        _index_only_after_backfill,
    )

    original_execute = AsyncConnection.execute
    updates_done = 0

    async def pause_after_first_batch_update(self, statement, *args, **kwargs):
        nonlocal updates_done
        result = await original_execute(self, statement, *args, **kwargs)
        if "UPDATE statute AS s" in str(statement) and "canonical_name_key" in str(statement):
            updates_done += 1
            if updates_done == 1:
                await asyncio.sleep(0.25)
        return result

    monkeypatch.setattr(AsyncConnection, "execute", pause_after_first_batch_update)

    async def count_filled() -> int:
        async with engine.connect() as other:
            return (
                await other.execute(
                    text(
                        "SELECT count(*) FROM statute "
                        "WHERE name LIKE 'Batch Commit Act %' AND canonical_name_key IS NOT NULL"
                    )
                )
            ).scalar_one()

    task = asyncio.create_task(mig.upgrade_post_commit())
    saw_partial = False
    for _ in range(200):
        await asyncio.sleep(0.01)
        filled = await count_filled()
        if 0 < filled < 4:
            saw_partial = True
            break
    await task
    assert saw_partial
    assert await count_filled() == 4


@pytest.mark.asyncio
async def test_migration_024_backfill_preserves_preset_set_before_update_in_batch(monkeypatch, db):
    mig = importlib.import_module(f"migrations.{_MIGRATION_024}")
    monkeypatch.setattr(mig, "_BATCH_SIZE", 2)
    await _drop_migration_024_schema()
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    row_one = Statute(name="Between Batch Row One, 1930", jurisdiction="Federal", year_enacted=1930)
    row_two = Statute(name="Between Batch Row Two, 1930", jurisdiction="Federal", year_enacted=1930)
    db.add(row_one)
    db.add(row_two)
    await db.commit()
    preset = "preset-before-update-in-batch"
    await db.close()

    original_execute = AsyncConnection.execute
    hooked = False

    async def execute_preset_before_first_update(self, statement, *args, **kwargs):
        nonlocal hooked
        stmt = str(statement)
        if "UPDATE statute AS s" in stmt and "canonical_name_key" in stmt and not hooked:
            hooked = True
            async with engine.begin() as hook_conn:
                await hook_conn.execute(
                    text(
                        "UPDATE statute SET canonical_name_key = :k WHERE name = :n"
                    ),
                    {"k": preset, "n": "Between Batch Row Two, 1930"},
                )
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", execute_preset_before_first_update)

    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        await mig._backfill_canonical_name_keys(conn)

    async with engine.connect() as conn:
        kept = (
            await conn.execute(
                text("SELECT canonical_name_key FROM statute WHERE name = :n"),
                {"n": "Between Batch Row Two, 1930"},
            )
        ).scalar()
        filled = (
            await conn.execute(
                text("SELECT canonical_name_key FROM statute WHERE name = :n"),
                {"n": "Between Batch Row One, 1930"},
            )
        ).scalar()
    assert kept == preset
    assert filled is not None


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
