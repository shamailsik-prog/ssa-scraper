"""Migration 018: scoped hash uniqueness and downgrade guards."""

from __future__ import annotations

import importlib
from datetime import date

import pytest
from sqlalchemy import select, text

from scraper.database import engine
from scraper.models import Court, Judgment


@pytest.mark.asyncio
async def test_migration_018_upgrade_rejects_duplicate_normalized_docket_keys(db):
    mig = importlib.import_module("migrations.018_judgment_case_text_hash")
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "x" * 600
    h = "a" * 64
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 10",
            full_text=body,
            full_text_hash=h,
            court_id=sc.id,
            docket_number="C.P. 1 of 2024",
            docket_key="c.p.1of2024",
            decision_date=date(2024, 1, 1),
            reporter="PLD",
        )
    )
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 11",
            full_text=body,
            full_text_hash=h,
            court_id=sc.id,
            docket_number="C.P.  1   of 2024",
            docket_key="c.p.1of2024",
            decision_date=date(2024, 1, 1),
            reporter="PLD",
        )
    )
    await db.commit()
    async with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="duplicate"):
            await mig.upgrade(conn)


@pytest.mark.asyncio
async def test_migration_018_downgrade_rejects_duplicate_full_text_hash(db):
    mig = importlib.import_module("migrations.018_judgment_case_text_hash")
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    h = "b" * 64
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 20",
            full_text="one " * 200,
            full_text_hash=h,
            court_id=sc.id,
            docket_number="A",
            docket_key="a",
            decision_date=date(2024, 1, 1),
        )
    )
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 21",
            full_text="two " * 200,
            full_text_hash=h,
            court_id=sc.id,
            docket_number="B",
            docket_key="b",
            decision_date=date(2024, 1, 2),
        )
    )
    await db.commit()
    async with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="duplicate full_text_hash"):
            await mig.downgrade(conn)


@pytest.mark.asyncio
async def test_migration_018_unique_constraint_uses_docket_key(db):
    mig = importlib.import_module("migrations.018_judgment_case_text_hash")
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
        await mig.upgrade(conn)
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    h = "c" * 64
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 30",
            full_text="t " * 200,
            full_text_hash=h,
            court_id=sc.id,
            docket_number="C.P. 1 of 2024",
            docket_key="c.p.1of2024",
            decision_date=date(2024, 1, 1),
        )
    )
    await db.commit()
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 31",
            full_text="t " * 200,
            full_text_hash=h,
            court_id=sc.id,
            docket_number="C.P.  1 of 2024",
            docket_key="c.p.1of2024",
            decision_date=date(2024, 1, 1),
        )
    )
    with pytest.raises(Exception):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_migration_018_m20_whitespace_docket_yields_null_docket_key(db):
    """M20: whitespace-only docket_number must not produce a docket_key after 018 upgrade."""
    mig = importlib.import_module("migrations.018_judgment_case_text_hash")
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE judgment DROP CONSTRAINT IF EXISTS uq_judgment_case_text_hash"))
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = "z" * 600
    h = "d" * 64
    row = Judgment(
        canonical_citation="PLD 2024 SC 40",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        docket_number="   ",
        docket_key=None,
        decision_date=date(2024, 3, 1),
        reporter="PLD",
    )
    db.add(row)
    await db.commit()
    row_id = row.id
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    db.expire_all()
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == row_id))).scalars().one()
    assert refreshed.docket_key is None
