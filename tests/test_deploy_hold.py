"""While a deploy waits for the PakistanLawSite job (Redis deploy_hold_login), no new PakistanLawSite work
starts and everything else carries on (8 October 2026: stopping celery-beat for each deferred deploy left
nothing scheduled for up to 45 minutes per push)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from scraper.models import ScraperSource
from scraper.tasks.chain import _redis
from scraper.tasks.deploy_hold import LOGIN_HOLD_KEY, login_work_held


@pytest.fixture()
def hold():
    _redis().set(LOGIN_HOLD_KEY, "1", ex=60)
    yield
    _redis().delete(LOGIN_HOLD_KEY)


def test_hold_follows_the_redis_key(hold):
    assert login_work_held() is True
    _redis().delete(LOGIN_HOLD_KEY)
    assert login_work_held() is False


async def test_dispatch_skips_pakistanlawsite_but_queues_public_sources_during_a_hold(db, monkeypatch, hold):
    from scraper.tasks.celery_app import app
    from scraper.tasks.dispatcher import dispatch_due_sources

    now = datetime.now(timezone.utc)
    for s in (await db.execute(select(ScraperSource))).scalars().all():
        s.next_scrape_at = now + timedelta(hours=1)
        if s.source_name in ("PakistanLawSite", "PakistanCode"):
            s.next_scrape_at = now - timedelta(minutes=1)
            s.state, s.is_active = "ACTIVE", True
    await db.commit()
    sent = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: sent.append((name, args)))
    result = await dispatch_due_sources()
    assert "PakistanLawSite:deploy_hold" in result["skipped_saturated"]
    assert all("login_session" not in n for n, _ in sent)
    assert ("scraper.tasks.dispatcher.run_source_job", ("PakistanCode",)) in sent


async def test_a_queued_pakistanlawsite_job_does_not_start_during_a_hold(db, hold):
    from scraper.tasks.dispatcher import run_source

    assert (await run_source("PakistanLawSite"))["skipped"] == "deploy_hold"


def test_case_id_walk_and_search_harvest_ticks_wait_during_a_hold(hold):
    from scraper.tasks.pls_caseid_walk import pls_caseid_walk_tick
    from scraper.tasks.pls_search_harvest import pls_search_harvest_tick

    assert pls_caseid_walk_tick()["skipped"] == "deploy_hold"
    assert pls_search_harvest_tick()["reason"] == "deploy_hold"
