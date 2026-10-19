"""B2 migration downgrade guards."""

from __future__ import annotations

from datetime import date

import importlib
import pytest
from sqlalchemy import select, text

from scraper.database import engine
from scraper.models import Court, Judgment, JudgmentIdentityLink


@pytest.mark.asyncio
async def test_migration_019_downgrade_refuses_when_link_rows_exist(db):
    mig = importlib.import_module("migrations.019_judgment_identity_reconcile")
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    u = Judgment(
        canonical_citation="UNREPORTED mig19",
        full_text="t" * 600,
        full_text_hash="1" * 64,
        court_id=sc.id,
        unreported=True,
    )
    r = Judgment(
        canonical_citation="PLD 2024 SC 99",
        full_text="t" * 600,
        full_text_hash="2" * 64,
        court_id=sc.id,
        unreported=False,
    )
    db.add_all([u, r])
    await db.flush()
    db.add(JudgmentIdentityLink(unreported_judgment_id=u.id, reported_judgment_id=r.id, source="test"))
    await db.commit()
    async with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="judgment_identity_link has rows"):
            await mig.downgrade(conn)


@pytest.mark.asyncio
async def test_migration_020_downgrade_refuses_duplicate_four_column_key(db):
    mig = importlib.import_module("migrations.020_judgment_case_hash_unreported_scope")
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    h = "3" * 64
    db.add(
        Judgment(
            canonical_citation="UNREPORTED dup",
            full_text="x" * 600,
            full_text_hash=h,
            court_id=sc.id,
            docket_key="dk",
            decision_date=date(2024, 1, 1),
            unreported=True,
        )
    )
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC dup",
            full_text="x" * 600,
            full_text_hash=h,
            court_id=sc.id,
            docket_key="dk",
            decision_date=date(2024, 1, 1),
            unreported=False,
        )
    )
    await db.commit()
    async with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="duplicate"):
            await mig.downgrade(conn)
    restore = importlib.import_module("migrations.020_judgment_case_hash_unreported_scope")
    async with engine.begin() as conn:
        await restore.upgrade(conn)


@pytest.mark.asyncio
async def test_migration_021_downgrade_refuses_duplicate_four_column_key(db):
    mig = importlib.import_module("migrations.021_repair_case_hash_unreported_scope")
    async with engine.begin() as conn:
        defn = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'uq_judgment_case_text_hash'"
                )
            )
        ).scalar()
        if defn and "unreported" not in (defn or "").lower():
            await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
            await conn.execute(
                text(
                    "ALTER TABLE judgment ADD CONSTRAINT uq_judgment_case_text_hash "
                    "UNIQUE (court_id, docket_key, decision_date, full_text_hash, unreported)"
                )
            )
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    h = "4" * 64
    db.add(
        Judgment(
            canonical_citation="UNREPORTED dup21",
            full_text="y" * 600,
            full_text_hash=h,
            court_id=sc.id,
            docket_key="dk2",
            decision_date=date(2024, 2, 1),
            unreported=True,
        )
    )
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC dup21",
            full_text="y" * 600,
            full_text_hash=h,
            court_id=sc.id,
            docket_key="dk2",
            decision_date=date(2024, 2, 1),
            unreported=False,
        )
    )
    await db.commit()
    async with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="duplicate"):
            await mig.downgrade(conn)
    restore = importlib.import_module("migrations.021_repair_case_hash_unreported_scope")
    async with engine.begin() as conn:
        await restore.upgrade(conn)
