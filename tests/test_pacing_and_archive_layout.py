"""Pacing guards retained from the governing prompt (LOGIN_DELAY_*, PAGES_PER_*) and the archive folder layout."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.config import Settings, settings
from scraper.models import CrawlFrontier, Judgment
from scraper.storage.archive import judgment_prefix
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
from tests.test_auth_playwright import _activate, _script_with_results

BASE = dict(DATABASE_URL="postgresql+asyncpg://x:y@localhost/db", REDIS_URL="redis://localhost/0")


def test_pacing_settings_validated():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(**BASE, LOGIN_DELAY_MIN=5, LOGIN_DELAY_MAX=2)
    with pytest.raises(ValidationError):
        Settings(**BASE, PAGES_PER_DAY=0)
    s = Settings(**BASE)
    assert (s.LOGIN_DELAY_MIN, s.LOGIN_DELAY_MAX, s.PAGES_PER_HOUR, s.PAGES_PER_DAY) == (4.0, 9.0, 300, 2500)


async def test_daily_page_budget_pauses_run_and_delays_between_pages(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD")
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", datetime.now().year)
    monkeypatch.setattr(settings, "PAGES_PER_DAY", 3)
    monkeypatch.setattr(settings, "LOGIN_DELAY_MIN", 4.0)
    monkeypatch.setattr(settings, "LOGIN_DELAY_MAX", 4.0)
    await _activate(db, login_source)
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    sc = _script_with_results(5)
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=sc.factory(), redis_client=r, sleep=fake_sleep)
    stats = await pipeline.run(max_queries=3, max_probes_per_volume=50)
    await db.commit()
    await r.aclose()
    assert stats["pacing_paused"] is True and stats["pages_charged"] == 4  # the 4th page trips the budget
    assert slept == [4.0, 4.0, 4.0]  # LOGIN_DELAY between the pages that ran
    fr = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.tier == 1))).scalars().first()
    assert fr.status == "pending" and fr.last_error.startswith("pacing")
    assert login_source.state == "ACTIVE"  # not halted, not paused: Beat resumes it on the next window
    assert (login_source.config_json or {}).get("pacing_slot_1", {}).get("day_pages") == 4  # counters are per login slot


def test_archive_layout_reported_vs_unreported():
    j = Judgment(canonical_citation="PLD 2024 SC 101", reporter="PLD", year=2024, court_name="Supreme Court of Pakistan")
    assert judgment_prefix(j) == "Citations/PLD/2024/PLD_2024_SC_101"
    u = Judgment(canonical_citation="W.P. 123/2024", reporter=None, year=2024, court_name="Lahore High Court")
    assert judgment_prefix(u) == "Unreported/Lahore_High_Court/2024/W_P_123_2024"

