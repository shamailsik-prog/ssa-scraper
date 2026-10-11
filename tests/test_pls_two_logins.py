"""Operator decision of 11 Oct 2026: PakistanLawSite login 1 and login 2 are separate accounts and run at the same
time (two reporter shards, one per login, each paced at its own 300 pages an hour). Two browsers on ONE login end
each other, so every job that opens a browser must hold that login's slot lock: the search harvest and the case-ID
walk used to open whichever login was current without it, and two of either could run at once over shared state."""

from __future__ import annotations

from pathlib import Path

import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.auth.session_manager import SessionLock
from scraper.config import settings
from scraper.models import BrowserSessionSlot, ScraperSource
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline, claim_free_slot

SLOT_KEYS = ("corpus:login_session_lock:PakistanLawSite:slot1", "corpus:login_session_lock:PakistanLawSite:slot2")


async def _pipeline(db, active_slots):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    for row in (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite"))).scalars().all():
        if row.slot_number in active_slots:
            row.state = "ACTIVE"
            row.storage_state_encrypted = settings.encrypt_value("{}")
    await db.flush()
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete(*SLOT_KEYS)
    return PakistanLawSitePipeline(db, source, redis_client=r), r


async def test_claim_free_slot_takes_the_login_nobody_holds(db):
    pipeline, r = await _pipeline(db, (1, 2))
    other = SessionLock("PakistanLawSite:slot1", r, max_holders=1)
    await other.acquire()
    try:
        slot = await claim_free_slot(pipeline)
        assert slot is not None and slot.slot_number == 2
        assert pipeline.runner.preferred_slot_number == 2
        assert pipeline.runner.exclusive_slot is True  # never fails over onto the login the other job holds
        assert pipeline._slot_lock is not None
        await pipeline._slot_lock.release()
    finally:
        await other.release()
        await r.delete(*SLOT_KEYS)
        await r.aclose()


async def test_claim_free_slot_returns_none_when_both_logins_are_in_use(db):
    pipeline, r = await _pipeline(db, (1, 2))
    held = [SessionLock(f"PakistanLawSite:slot{n}", r, max_holders=1) for n in (1, 2)]
    for lock in held:
        await lock.acquire()
    try:
        assert await claim_free_slot(pipeline) is None
        assert pipeline._slot_lock is None
    finally:
        for lock in held:
            await lock.release()
        await r.delete(*SLOT_KEYS)
        await r.aclose()


async def test_claim_free_slot_ignores_a_login_that_is_not_active(db):
    pipeline, r = await _pipeline(db, (1,))
    held = SessionLock("PakistanLawSite:slot1", r, max_holders=1)
    await held.acquire()
    try:
        assert await claim_free_slot(pipeline) is None  # slot 2 is not signed in: never opened
    finally:
        await held.release()
        await r.delete(*SLOT_KEYS)
        await r.aclose()


def test_search_harvest_and_caseid_walk_hold_a_login_and_run_one_at_a_time():
    for path in ("scraper/tasks/pls_search_harvest.py", "scraper/tasks/pls_caseid_walk.py"):
        text = Path(path).read_text(encoding="utf-8")
        assert "claim_free_slot(" in text, path
        assert "no free login slot" in text, path
    assert 'SessionLock(f"{SOURCE_NAME}:search_harvest"' in Path("scraper/tasks/pls_search_harvest.py").read_text(encoding="utf-8")
    assert 'SessionLock(f"{SOURCE_NAME}:caseid_walk"' in Path("scraper/tasks/pls_caseid_walk.py").read_text(encoding="utf-8")


def test_installer_and_deploy_run_both_logins():
    install = Path("cloud/install.sh").read_text(encoding="utf-8")
    assert "LOGIN_SESSION_CONCURRENCY 2->1" not in install
    deploy = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    assert "LOGIN_SESSION_CONCURRENCY=2" in deploy
    assert "BACKFILL_LOGIN_SESSION_CONCURRENCY=2" in deploy


def test_deploy_step_sets_both_logins_in_env(tmp_path):
    import subprocess

    env = tmp_path / ".env"
    env.write_text("FOO=1\nLOGIN_SESSION_CONCURRENCY=1\nBAR=2\n", encoding="utf-8")
    script = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    start = script.index("ensure_two_pls_logins() {")
    func = script[start : script.index("\n}\n", start) + 3]
    prog = 'log() { echo "$*"; }\n' + func + 'cd "$1" && ensure_two_pls_logins && ensure_two_pls_logins\n'
    subprocess.run(["bash", "-c", prog, "deploy-test", str(tmp_path)], check=True, capture_output=True, text=True)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["FOO=1", "LOGIN_SESSION_CONCURRENCY=2", "BAR=2", "BACKFILL_LOGIN_SESSION_CONCURRENCY=2"]
