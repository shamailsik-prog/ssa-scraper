"""Sources the owner retired stay as DISABLED rows and are never dispatched, on every database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from scraper.config import settings
from scraper.database import seed_data
from scraper.models import ScraperSource
from scraper.tasks.dispatcher import dispatch_due_sources


async def _sources(db) -> dict:
    db.expire_all()
    return {s.source_name: s for s in (await db.execute(select(ScraperSource))).scalars().all()}


async def test_retired_sources_are_disabled_and_never_dispatched(db, monkeypatch):
    from scraper.tasks.celery_app import app

    monkeypatch.setattr(settings, "RETIRED_SOURCES", "NasirLawSite,BalochistanHighCourt")
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    rows = await _sources(db)
    for name in ("NasirLawSite", "BalochistanHighCourt", "PakistanCode"):
        rows[name].state, rows[name].is_active = "HALTED" if name != "PakistanCode" else "ACTIVE", True
        rows[name].next_scrape_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    await seed_data()  # runs at every API start

    rows = await _sources(db)
    for name in ("NasirLawSite", "BalochistanHighCourt"):
        assert rows[name].state == "DISABLED" and rows[name].is_active is False
        assert "retired" in rows[name].state_reason
    assert rows["PakistanCode"].state == "ACTIVE"  # nothing else is touched

    queued = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: queued.append(args))
    await dispatch_due_sources()
    names = {a[0] for a in queued if a}
    assert "PakistanCode" in names and not names & {"NasirLawSite", "BalochistanHighCourt"}


async def test_clearing_the_setting_leaves_a_source_alone(db, monkeypatch):
    monkeypatch.setattr(settings, "RETIRED_SOURCES", "")
    await seed_data()
    rows = await _sources(db)
    assert rows["NasirLawSite"].state != "DISABLED"



async def test_a_retired_source_re_enabled_on_the_dashboard_is_still_never_run(db, monkeypatch):
    """Resume/Enable on the dashboard cannot bring a retired source back until the setting changes."""
    from scraper.tasks.celery_app import app
    from scraper.tasks.dispatcher import run_source

    monkeypatch.setattr(settings, "RETIRED_SOURCES", "NasirLawSite")
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    rows = await _sources(db)
    rows["NasirLawSite"].state, rows["NasirLawSite"].is_active = "ACTIVE", True  # as after Enable + Resume
    rows["NasirLawSite"].next_scrape_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    queued = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: queued.append(args))
    await dispatch_due_sources()
    assert "NasirLawSite" not in {a[0] for a in queued if a}  # the scheduler skips it
    assert await run_source("NasirLawSite") == {"skipped": "retired"}  # a job already queued does nothing


def test_run_now_on_a_retired_source_is_refused(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "RETIRED_SOURCES", "NasirLawSite")
    r = client.post("/admin/sources/NasirLawSite/trigger", headers=admin_headers)
    assert r.status_code == 409 and "retired" in r.json()["detail"]
