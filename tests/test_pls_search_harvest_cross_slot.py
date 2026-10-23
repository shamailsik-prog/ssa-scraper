"""Cross-slot idle claim for PLS search harvest (flagged; default off)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.config import Settings, settings
from scraper.database import SessionLocal
from scraper.models import BrowserSessionSlot, PlsSearchHarvestQuery, ScraperSource
from scraper.tasks import pls_search_harvest as harvest

SLOT1 = "PLD,SCMR,CLC,YLR,CLD,GBLR"


@pytest_asyncio.fixture
async def r(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", SLOT1)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TAKEOVER_MINUTES", 30)
    client = aioredis.from_url(settings.REDIS_URL)

    async def clear():
        keys = await client.keys("corpus:login_session_lock:PakistanLawSite*")
        if keys:
            await client.delete(*keys)

    await clear()
    yield client
    await clear()
    await client.aclose()


async def _seed(db, reporters, *, priority=100):
    rows = []
    for rep in reporters:
        row = PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key=f"search:reporter={rep}|year=2026",
            query_json={"reporter": rep, "year": 2026},
            status="pending",
            priority=priority,
        )
        db.add(row)
        rows.append(row)
    await db.commit()
    return rows


async def _slots_active(db):
    now = datetime.now(timezone.utc)
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    source.state = "ACTIVE"
    for row in (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite"))).scalars().all():
        if row.slot_number in (1, 2):
            row.state = "ACTIVE"
            row.storage_state_encrypted = settings.encrypt_value("{}")
            row.last_used_at = row.last_verified_at = now
    await db.commit()


def test_mutant_cross_slot_claim_flag_default_off():
    assert Settings.model_fields["PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM"].default is False


async def test_mutant_cross_slot_idle_claim_requires_flag(db, r, monkeypatch):
    await _slots_active(db)
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", False)
    assert await harvest.cross_slot_idle_claim_allowed(db, 2) is False
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", True)
    assert await harvest.cross_slot_idle_claim_allowed(db, 2) is True


async def test_flag_off_idle_slot_does_not_steal_other_share(db, r, monkeypatch):
    await _slots_active(db)
    await _seed(db, ["PLD"])
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", False)
    row, lock, took_over, cross_slot = await harvest.try_claim_harvest_query_for_slot(db, 2, redis_client=r)
    assert row is None and not took_over and not cross_slot
    if lock:
        await lock.release()


async def test_idle_slot_claims_other_share_while_both_active(db, r, monkeypatch):
    await _slots_active(db)
    await _seed(db, ["PLD"])
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", True)
    row, lock, took_over, cross_slot = await harvest.try_claim_harvest_query_for_slot(db, 2, redis_client=r)
    assert row is not None
    assert row.query_json["reporter"] == "PLD"
    assert cross_slot is True
    assert took_over is False
    await lock.release()


async def test_own_share_always_claimed_before_cross_slot(db, r, monkeypatch):
    await _slots_active(db)
    await _seed(db, ["PLD", "MLD"])
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", True)
    row, lock, _, cross_slot = await harvest.try_claim_harvest_query_for_slot(db, 1, redis_client=r)
    assert row is not None and row.query_json["reporter"] == "PLD"
    assert cross_slot is False
    await lock.release()


async def test_concurrent_cross_slot_claimers_never_double_claim(db, r, monkeypatch):
    """Two slots competing for the other's idle share still claim distinct rows."""
    await _slots_active(db)
    await _seed(db, [f"R{n}" for n in range(10)])
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", True)
    claimed = {1: [], 2: []}
    locks = []

    async def picker(slot):
        async with SessionLocal() as s:
            while True:
                row, lock, _, _ = await harvest.try_claim_harvest_query_for_slot(s, slot, redis_client=r)
                if row is None:
                    return
                claimed[slot].append(row.id)
                locks.append(lock)
                await asyncio.sleep(0)

    await asyncio.gather(picker(1), picker(2))
    every = claimed[1] + claimed[2]
    assert len(every) == 10 and len(set(every)) == 10
    for lock in locks:
        await lock.release()


async def test_active_other_slot_in_progress_is_not_borrowed(db, r, monkeypatch):
    """An active login's resumable in_progress row is not idle pending work."""
    await _slots_active(db)
    rows = await _seed(db, ["PLD"])
    rows[0].status = "in_progress"
    await db.commit()
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", True)
    row, lock, took_over, cross_slot = await harvest.try_claim_harvest_query_for_slot(db, 2, redis_client=r)
    assert row is None and lock is None
    assert not took_over and not cross_slot
    await db.refresh(rows[0])
    assert rows[0].status == "in_progress"
    assert rows[0].slot is None


async def test_active_cross_slot_selects_pending_not_higher_priority_in_progress(db, r, monkeypatch):
    """Pending other-share work must win even if an in_progress row sorts first."""
    await _slots_active(db)
    rows = await _seed(db, ["PLD", "SCMR"])
    rows[0].status = "in_progress"
    rows[0].priority = 1
    rows[1].priority = 100
    await db.commit()
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_CROSS_SLOT_CLAIM", True)
    row, lock, took_over, cross_slot = await harvest.try_claim_harvest_query_for_slot(db, 2, redis_client=r)
    assert row is not None and row.id == rows[1].id
    assert cross_slot and not took_over
    await lock.release()
    await db.refresh(rows[0])
    assert rows[0].status == "in_progress"
    assert rows[0].slot is None
