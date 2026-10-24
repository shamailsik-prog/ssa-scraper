"""Search harvest returns query to pending when Playwright dies mid-tick."""

from __future__ import annotations

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.auth.session_manager import BrowserDisconnected, SessionLock
from scraper.config import settings
from scraper.database import SessionLocal
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
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=PLD|year=2026",
        query_json={"reporter": "PLD", "year": 2026},
        status="pending",
        priority=100,
        rows_seen=7,
        rows_new=2,
        cursor_json={"page": 1, "row_index": 3},
    )
    db.add(q)
    await db.commit()
    return q


@pytest.mark.asyncio
async def test_run_search_harvest_browser_disconnect_pending_and_lock_released(db, r, monkeypatch):
    async with SessionLocal() as db:
        query = await _ready_slot(db)
        qid = query.id

    async def active_map(db, source_name):
        return object()

    async def run_query(self, query_row, search_map, *, max_pages=0):
        raise BrowserDisconnected("Target closed")

    monkeypatch.setattr(harvest, "active_map", active_map)
    monkeypatch.setattr(harvest, "map_as_dict", lambda m: {})
    monkeypatch.setattr(harvest.PakistanLawSitePipeline, "_is_dashboard_citation_fields", classmethod(lambda cls, m: True))
    monkeypatch.setattr(harvest.SearchHarvestRunner, "run_query", run_query)

    async with SessionLocal() as db:
        result = await harvest.run_search_harvest(db, slot=1, redis_client=r)
    assert result.get("browser_fatal") is True
    assert result.get("paused") is True

    async with SessionLocal() as db:
        row = await db.get(PlsSearchHarvestQuery, qid)
        assert row.status == "pending"
        assert row.rows_seen == 7
        assert row.rows_new == 2
        assert row.cursor_json == {"page": 1, "row_index": 3}

    probe = SessionLock("PakistanLawSite:search_harvest:slot1", r, max_holders=1)
    await probe.acquire()
    await probe.release()
