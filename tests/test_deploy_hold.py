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



async def test_a_held_pakistanlawsite_job_stays_due_for_the_first_dispatch_after_the_hold(db, hold):
    from scraper.tasks.dispatcher import run_source

    src = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    src.next_scrape_at = datetime.now(timezone.utc) + timedelta(hours=1)  # as the dispatcher left it
    await db.commit()
    assert (await run_source("PakistanLawSite"))["skipped"] == "deploy_hold"
    db.expire_all()
    src = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    assert src.next_scrape_at <= datetime.now(timezone.utc)


def test_periodic_pakistanlawsite_tasks_wait_during_a_hold(hold):
    from scraper.tasks.login_recovery import recover_login_slots
    from scraper.tasks.pls_self_healing import pls_keepalive_hourly, pls_stall_watchdog
    from scraper.tasks.spot_check import spot_check_judgments

    for task in (recover_login_slots, pls_keepalive_hourly, pls_stall_watchdog, spot_check_judgments):
        assert task()["skipped"] == "deploy_hold", task.name


def test_host_keepalive_cli_path_waits_during_a_hold(hold, monkeypatch):
    """The host backup cron runs `python -m scraper.tasks.pls_self_healing`, not the Celery task."""
    from scraper.config import settings
    from scraper.database import run_async
    from scraper.tasks.pls_self_healing import keepalive_pakistanlawsite_slots

    monkeypatch.setattr(settings, "ALLOW_LOGIN_SCRAPING", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "chambers")
    assert run_async(keepalive_pakistanlawsite_slots())["skipped"] == "deploy_hold"


def test_worker_version_probe_requires_the_complete_guard_set():
    """auto_deploy.sh asks the running worker for HOLD_GUARDS_VERSION; the first hold (no attribute, read as 1)
    did not guard slot recovery, keepalive, the watchdog or the spot check, so it must not pass."""
    import subprocess
    from pathlib import Path

    from scraper.tasks import deploy_hold

    lib = (Path(__file__).resolve().parents[1] / "scripts" / "pls_host_lib.sh").read_text()
    minimum = int(lib.split("PLS_HOST_LOGIN_HOLD_MIN_VERSION=")[1].split()[0])
    assert minimum >= 2 and deploy_hold.HOLD_GUARDS_VERSION >= minimum
    probe = f"import sys, scraper.tasks.deploy_hold as h; sys.exit(0 if getattr(h, 'HOLD_GUARDS_VERSION', 1) >= {minimum} else 1)"
    assert subprocess.run(["python", "-c", probe], cwd=Path(__file__).resolve().parents[1]).returncode == 0
