"""Migration 012: the public sources paused for the PakistanLawSite-only focus run again (7 October 2026)."""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

from sqlalchemy import select

from scraper.database import engine
from scraper.models import ScraperSource

migration = importlib.import_module("migrations.012_resume_public_sources")


async def _set(db, name, state, reason, active=True):
    s = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == name))).scalars().first()
    s.state, s.state_reason, s.is_active = state, reason, active
    return s


async def test_focus_pauses_resume_pakistancode_first_blocks_stay(db):
    await _set(db, "PakistanCode", "PAUSED", "Shamail PLS-only focus; PakistanCode deferred")
    await _set(db, "SupremeCourt", "PAUSED", "dual-track only (Shamail 2026-09-21 ~08:34 PKT): PLS+PakistanCode ACTIVE", active=False)
    await _set(db, "SindhHighCourt", "PAUSED", "dual-track only (Shamail 2026-09-21 ~08:34 PKT): PLS+PakistanCode ACTIVE", active=False)
    await _set(db, "BalochistanHighCourt", "HALTED", "block: access denied")
    await _set(db, "LahoreHighCourt", "PAUSED", "admin pause for maintenance")
    await db.commit()

    before = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await migration.upgrade(conn)
    db.expire_all()
    rows = {s.source_name: s for s in (await db.execute(select(ScraperSource))).scalars().all()}

    for name in ("PakistanCode", "SupremeCourt", "SindhHighCourt"):
        assert rows[name].state == "ACTIVE" and rows[name].is_active, name
        assert "operator instruction" in rows[name].state_reason
    assert rows["PakistanCode"].next_scrape_at <= rows["SindhHighCourt"].next_scrape_at <= rows["SupremeCourt"].next_scrape_at
    assert (rows["PakistanCode"].next_scrape_at - before).total_seconds() < 60  # due at once
    assert rows["BalochistanHighCourt"].state == "HALTED"  # an explicit block is never lifted here
    assert rows["LahoreHighCourt"].state == "PAUSED"  # paused for another reason: left alone
