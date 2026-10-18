"""Owner decision 16 Oct 2026: PakistanLawSite Account 1 is acquisition and Account 2 is independent
verification. They may be live concurrently, but acquisition helpers must never borrow the verifier account."""

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
    async def _nosleep(*_a, **_k):
        return None

    return PakistanLawSitePipeline(db, source, redis_client=r, sleep=_nosleep), r


async def test_claim_free_slot_never_borrows_verifier_when_harvester_is_busy(db):
    pipeline, r = await _pipeline(db, (1, 2))
    other = SessionLock("PakistanLawSite:slot1", r, max_holders=1)
    await other.acquire()
    try:
        assert await claim_free_slot(pipeline) is None
        assert pipeline.runner.preferred_slot_number == 1
        assert pipeline.runner.exclusive_slot is True
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
    walk = Path("scraper/tasks/pls_caseid_walk.py").read_text(encoding="utf-8")
    assert "claim_free_slot(" in walk and "no free login slot" in walk
    assert 'SessionLock(f"{SOURCE_NAME}:caseid_walk"' in walk
    # Account 1 is the only acquisition login; its search/case-ID paths still hold the slot lock.
    harvest = Path("scraper/tasks/pls_search_harvest.py").read_text(encoding="utf-8")
    assert 'SessionLock(f"{SOURCE_NAME}:search_harvest:slot{n}"' in harvest
    assert 'SessionLock(f"{SOURCE_NAME}:slot{chosen}"' in harvest


def test_installer_and_deploy_run_both_logins():
    install = Path("cloud/install.sh").read_text(encoding="utf-8")
    assert "LOGIN_SESSION_CONCURRENCY 2->1" not in install
    deploy = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    assert "LOGIN_SESSION_CONCURRENCY=2" in deploy
    assert "BACKFILL_LOGIN_SESSION_CONCURRENCY=2" in deploy


def test_deploy_step_sets_both_logins_in_env(tmp_path):
    import subprocess

    env = tmp_path / ".env"
    env.write_text("FOO=1\nLOGIN_SESSION_CONCURRENCY=1\nBAR=2", encoding="utf-8")  # no final newline
    script = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    func = ""
    for name in ("pin_env() {", "ensure_two_pls_logins() {"):
        start = script.index(name)
        func += script[start : script.index("\n}\n", start) + 3]
    prog = 'log() { echo "$*"; }\n' + func + 'cd "$1" && ensure_two_pls_logins && ensure_two_pls_logins\n'
    subprocess.run(["bash", "-c", prog, "deploy-test", str(tmp_path)], check=True, capture_output=True, text=True)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["FOO=1", "LOGIN_SESSION_CONCURRENCY=2", "BAR=2", "BACKFILL_LOGIN_SESSION_CONCURRENCY=2"]


async def test_a_crashed_shared_holder_drops_out_on_its_own(monkeypatch):
    """Review on #192: one live holder refreshing the shared key kept a crashed holder's place for ever."""
    import asyncio

    import scraper.auth.session_manager as sm
    from scraper.auth.session_manager import SessionLockHeld

    r = aioredis.from_url(settings.REDIS_URL)
    key = "corpus:login_session_lock:PakistanLawSite:holders"
    await r.delete(key, "corpus:login_session_lock:PakistanLawSite")
    monkeypatch.setattr(sm, "LOCK_TTL_SECONDS", 1)
    crashed = SessionLock("PakistanLawSite", r, max_holders=2)
    alive = SessionLock("PakistanLawSite", r, max_holders=2)
    try:
        await crashed.acquire()
        await alive.acquire()
        third = SessionLock("PakistanLawSite", r, max_holders=2)
        try:
            await third.acquire()
            raise AssertionError("both places are taken")
        except SessionLockHeld:
            pass
        for _ in range(3):  # the live holder keeps refreshing; the crashed one never does
            await asyncio.sleep(0.45)
            await alive.refresh()
        newcomer = SessionLock("PakistanLawSite", r, max_holders=2)
        await newcomer.acquire()  # the crashed holder's place is free again
        await newcomer.release()
        await alive.release()
    finally:
        await r.delete(key)
        await r.aclose()


async def test_shared_lock_reads_the_older_plain_set_format():
    r = aioredis.from_url(settings.REDIS_URL)
    key = "corpus:login_session_lock:PakistanLawSite:holders"
    await r.delete(key, "corpus:login_session_lock:PakistanLawSite")
    await r.sadd(key, "holder-from-the-previous-release")
    try:
        lock = SessionLock("PakistanLawSite", r, max_holders=2)
        await lock.acquire()  # one place left: the old holder keeps its own
        assert await r.type(key) in (b"zset", "zset")
        assert await r.zcard(key) == 2
        await lock.release()
        assert await r.zcard(key) == 1
    finally:
        await r.delete(key)
        await r.aclose()


async def test_run_locks_are_kept_alive_with_every_page(db):
    pipeline, r = await _pipeline(db, (1,))
    calls = {"n": 0}

    class RunLock:
        async def refresh(self):
            calls["n"] += 1

    pipeline._run_locks.append(RunLock())
    try:
        await pipeline._charge_page()
    except Exception:
        pass
    await r.aclose()
    assert calls["n"] == 1


async def test_claim_free_slot_always_uses_account1_even_when_account2_has_more_budget(db):
    from datetime import datetime, timezone
    from scraper.tasks.pakistanlawsite import pacing_key

    pipeline, r = await _pipeline(db, (1, 2))
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    cfg = dict(pipeline.source.config_json or {})
    cfg[pacing_key(1)] = {"hour": hour, "hour_pages": 290}
    cfg[pacing_key(2)] = {"hour": hour, "hour_pages": 10}
    pipeline.source.config_json = cfg
    await db.flush()
    try:
        slot = await claim_free_slot(pipeline)
        assert slot is not None and slot.slot_number == 1
        assert pipeline.runner.preferred_slot_number == 1
        await pipeline._slot_lock.release()
    finally:
        await r.delete(*SLOT_KEYS)
        await r.aclose()

