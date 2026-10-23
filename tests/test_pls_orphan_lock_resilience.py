"""PLS search-harvest Redis lock resilience (orphaned locks after worker recreate)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as aioredis

from scraper.auth.session_manager import LOCK_KEY, SessionLock, SessionLockHeld
from scraper.config import settings
from scraper.pls_grid_health import compute_stalled, release_pls_search_harvest_redis_locks
from scraper.pls_lock_holder import lock_holder_token, lock_value_holder_dead, parse_lock_holder
from scraper.pls_lock_registry import register_lock, release_all_registered_sync


@pytest.fixture
async def r():
    client = aioredis.from_url(settings.REDIS_URL)
    yield client
    await client.aclose()


def test_parse_lock_holder_roundtrip():
    token = lock_holder_token()
    parsed = parse_lock_holder(token)
    assert parsed is not None
    host, pid, nonce = parsed
    assert host and pid == os.getpid() and len(nonce) == 16


def test_lock_value_holder_dead_for_foreign_host():
    token = f"dead-container:{os.getpid()}:abc"
    assert lock_value_holder_dead(token) is True


@pytest.mark.asyncio
async def test_dead_holder_takeover_acquires_slot_lock(r, monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", True)
    source = "PakistanLawSite:search_harvest:slot9"
    key = LOCK_KEY.format(source=source)
    await r.delete(key)
    stale = f"gone-host:{999999}:deadbeefdeadbeef"
    await r.set(key, stale, ex=60)

    lock = SessionLock(source, r, holder_identity=True)
    await lock.acquire(dead_holder_takeover=True)
    try:
        raw = await r.get(key)
        assert raw is not None
        assert parse_lock_holder(raw.decode()) is not None
        assert raw.decode() != stale
    finally:
        await lock.release()
    await r.delete(key)


@pytest.mark.asyncio
async def test_dead_holder_takeover_does_not_steal_live_lock(r, monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", True)
    source = "PakistanLawSite:search_harvest:slot8"
    key = LOCK_KEY.format(source=source)
    await r.delete(key)
    holder = SessionLock(source, r, holder_identity=True)
    await holder.acquire()
    try:
        other = SessionLock(source, r, holder_identity=True)
        with pytest.raises(SessionLockHeld):
            await other.acquire(dead_holder_takeover=True)
    finally:
        await holder.release()
    await r.delete(key)


def test_shutdown_registry_releases_tracked_lock(monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    import redis

    client = redis.from_url(settings.REDIS_URL)
    key = LOCK_KEY.format(source="PakistanLawSite:search_harvest:shutdown-test")
    token = lock_holder_token()
    client.set(key, token, ex=120)
    register_lock(key, token)
    released = release_all_registered_sync()
    assert released == 1
    assert client.get(key) is None
    client.delete(key)
    client.close()


@pytest.mark.asyncio
async def test_release_search_harvest_locks_respects_deploy_flag(r, monkeypatch):
    prefix_key = "corpus:login_session_lock:PakistanLawSite:search_harvest:slot-test"
    await r.set(prefix_key, "x", ex=60)
    monkeypatch.setattr(settings, "PLS_DEPLOY_RELEASE_SEARCH_HARVEST_LOCKS", False)
    assert await release_pls_search_harvest_redis_locks() == 0
    assert await r.exists(prefix_key)
    monkeypatch.setattr(settings, "PLS_DEPLOY_RELEASE_SEARCH_HARVEST_LOCKS", True)
    n = await release_pls_search_harvest_redis_locks()
    assert n >= 1
    assert not await r.exists(prefix_key)


def test_compute_stalled_locks_held_no_staging(monkeypatch):
    monkeypatch.setattr(settings, "PLS_STALL_LOCK_HELD_NO_STAGING_ENABLED", True)
    monkeypatch.setattr(settings, "PLS_STALL_LOCK_HELD_NO_STAGING_MINUTES", 15)
    now = datetime.now(timezone.utc)
    verdict = compute_stalled(
        last_promotion_at=now - timedelta(hours=1),
        now=now,
        search_harvest_locks_held=True,
        last_staging_at=now - timedelta(minutes=20),
    )
    assert verdict["stalled"] is True
    assert verdict["stalled_reason"] == "locks_held_no_staging"

    ok = compute_stalled(
        last_promotion_at=now - timedelta(hours=1),
        now=now,
        search_harvest_locks_held=True,
        last_staging_at=now - timedelta(minutes=5),
    )
    assert ok["stalled"] is False or ok.get("stalled_reason") != "locks_held_no_staging"
