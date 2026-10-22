"""B7/R1 K2: run_search_harvest skips when Postgres harvest advisory lock is held."""

from __future__ import annotations

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.auth.session_manager import SessionLock
from scraper.config import settings
from scraper.database import SessionLocal
from scraper.harvest_lock import harvest_run_pg_lock
from scraper.models import BrowserSessionSlot, PlsSearchHarvestQuery, ScraperSource
from scraper.tasks import pls_search_harvest as harvest


@pytest_asyncio.fixture
async def r(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", "PLD,SCMR,CLC,YLR,CLD,GBLR")
    client = aioredis.from_url(settings.REDIS_URL)
    keys = await client.keys("corpus:login_session_lock:PakistanLawSite*")
    if keys:
        await client.delete(*keys)
    yield client
    keys = await client.keys("corpus:login_session_lock:PakistanLawSite*")
    if keys:
        await client.delete(*keys)
    await client.aclose()


async def _ready_slot(db):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    source.state = "ACTIVE"
    for row in (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite"))).scalars().all():
        if row.slot_number == 1:
            row.state = "ACTIVE"
            row.storage_state_encrypted = settings.encrypt_value("{}")
    db.add(
        PlsSearchHarvestQuery(
            source_name="PakistanLawSite",
            query_key="search:reporter=PLD|year=2026",
            query_json={"reporter": "PLD", "year": 2026},
            status="pending",
            priority=100,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_run_search_harvest_skips_when_pg_advisory_lock_held_and_releases_redis_lock(db, r, monkeypatch):
    async with SessionLocal() as db:
        await _ready_slot(db)

    async def active_map(db, source_name):
        return object()

    async def run_query(self, query_row, search_map, *, max_pages=0):
        query_row.status = "done"
        return {"query_key": query_row.query_key, "status": "done"}

    monkeypatch.setattr(harvest, "active_map", active_map)
    monkeypatch.setattr(harvest, "map_as_dict", lambda m: {})
    monkeypatch.setattr(harvest.PakistanLawSitePipeline, "_is_dashboard_citation_fields", classmethod(lambda cls, m: True))
    monkeypatch.setattr(harvest.SearchHarvestRunner, "run_query", run_query)

    async with harvest_run_pg_lock(1) as held:
        assert held is True
        async with SessionLocal() as db:
            result = await harvest.run_search_harvest(db, slot=1, redis_client=r)
        assert result.get("skipped") is True
        assert "postgres harvest advisory lock" in (result.get("reason") or "").lower()

    # Redis harvest lock must not leak when PG lock refused the tick.
    probe = SessionLock("PakistanLawSite:search_harvest:slot1", r, max_holders=1)
    await probe.acquire()
    await probe.release()
