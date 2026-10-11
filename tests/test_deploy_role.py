"""DEPLOY_ROLE: one server harvests PakistanLawSite, another the free sites, and neither does the other's work."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.deploy_role import LOGIN_ONLY_BEAT_ENTRIES, PUBLIC_ONLY_BEAT_ENTRIES, beat_schedule_for_role, harvests
from scraper.models import ScraperSource
from scraper.pls_grid_health import compute_stalled
from scraper.tasks.dispatcher import dispatch_due_sources, run_source
from tests.test_dispatcher import _activate_pls_slots

SCHEDULE = {name: {"task": name} for name in (*LOGIN_ONLY_BEAT_ENTRIES, *PUBLIC_ONLY_BEAT_ENTRIES, "dispatch-due-sources", "promote-staging", "archive-mirror")}


@pytest.mark.parametrize(
    "role, login, public",
    [("all", True, True), ("pls", True, False), ("public", False, True)],
)
def test_role_decides_sources_and_beat_entries(monkeypatch, role, login, public):
    monkeypatch.setattr(settings, "DEPLOY_ROLE", role)
    assert harvests("login_session") is login and harvests("public") is public
    kept = set(beat_schedule_for_role(SCHEDULE))
    assert {"dispatch-due-sources", "promote-staging", "archive-mirror"} <= kept  # every server keeps its own corpus moving
    assert (LOGIN_ONLY_BEAT_ENTRIES <= kept) if login else not (LOGIN_ONLY_BEAT_ENTRIES & kept)
    assert (PUBLIC_ONLY_BEAT_ENTRIES <= kept) if public else not (PUBLIC_ONLY_BEAT_ENTRIES & kept)


async def _everything_due(db, monkeypatch):
    from scraper.harvest_mode import set_harvest_mode
    from scraper.tasks.celery_app import app

    now = datetime.now(timezone.utc)
    for s in (await db.execute(select(ScraperSource))).scalars().all():
        s.next_scrape_at, s.state, s.is_active = now - timedelta(minutes=1), "ACTIVE", True
    await _activate_pls_slots(db, (1, 2))
    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", False)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="deploy role")
    await db.commit()
    queued = []
    monkeypatch.setattr(app, "send_task", lambda name, args=(), kwargs=None, queue=None, **_: queued.append((name, args, queue)))
    return queued


async def test_free_sites_server_never_queues_pakistanlawsite(db, monkeypatch):
    queued = await _everything_due(db, monkeypatch)
    monkeypatch.setattr(settings, "DEPLOY_ROLE", "public")
    await dispatch_due_sources()
    assert queued, "public sources are still dispatched"
    assert all(q != "login_session" for _, _, q in queued)


async def test_pakistanlawsite_server_never_queues_free_sites(db, monkeypatch):
    queued = await _everything_due(db, monkeypatch)
    monkeypatch.setattr(settings, "DEPLOY_ROLE", "pls")
    await dispatch_due_sources()
    assert queued, "PakistanLawSite is still dispatched"
    assert all(q == "login_session" for _, _, q in queued)


async def test_a_job_that_reaches_the_wrong_server_is_not_run(db, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOY_ROLE", "pls")
    assert await run_source("PakistanCode") == {"skipped": "other_server", "deploy_role": "pls"}
    monkeypatch.setattr(settings, "DEPLOY_ROLE", "public")
    assert (await run_source("PakistanLawSite"))["skipped"] == "other_server"


def test_free_sites_server_is_never_stalled_on_pakistanlawsite(monkeypatch):
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    assert compute_stalled(last_promotion_at=long_ago)["stalled"] is True
    monkeypatch.setattr(settings, "DEPLOY_ROLE", "public")
    verdict = compute_stalled(last_promotion_at=long_ago)
    assert verdict["stalled"] is False and verdict["stalled_reason"] == "not_harvested_on_this_server"


def test_manual_trigger_of_the_other_servers_source_is_refused(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOY_ROLE", "public")
    r = client.post("/admin/sources/PakistanLawSite/trigger", headers=admin_headers)
    assert r.status_code == 409 and "other server" in r.json()["detail"]
