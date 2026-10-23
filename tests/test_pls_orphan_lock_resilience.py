"""PLS search-harvest Redis lock resilience (orphaned locks after worker recreate)."""

from __future__ import annotations

import atexit
import os
from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as aioredis

from scraper.auth.session_manager import LOCK_KEY, SessionLock, SessionLockHeld
from scraper.config import settings
from scraper.pls_grid_health import compute_stalled, release_pls_search_harvest_redis_locks
from scraper.pls_lock_holder import (
    lock_holder_token,
    lock_value_holder_stale,
    parse_lock_holder,
    touch_worker_alive,
    worker_alive_redis_key,
)
from scraper.pls_lock_registry import register_lock, release_all_registered_sync
from scraper.tasks.celery_app import _install_pls_lock_shutdown_hooks


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


@pytest.mark.asyncio
async def test_foreign_host_with_live_heartbeat_is_not_stale(r):
    host = "other-worker-container"
    token = f"{host}:99999:abcabcabcabcabca"
    await r.set(worker_alive_redis_key(host), "1", ex=60)
    assert await lock_value_holder_stale(r, token) is False
    await r.delete(worker_alive_redis_key(host))


@pytest.mark.asyncio
async def test_foreign_host_without_heartbeat_is_stale(r):
    token = "gone-container:99999:abcabcabcabcabca"
    await r.delete(worker_alive_redis_key("gone-container"))
    assert await lock_value_holder_stale(r, token) is True


@pytest.mark.asyncio
async def test_dead_holder_takeover_acquires_slot_lock(r, monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", True)
    source = "PakistanLawSite:search_harvest:slot9"
    key = LOCK_KEY.format(source=source)
    await r.delete(key)
    stale = f"gone-host:{999999}:deadbeefdeadbeef"
    await r.delete(worker_alive_redis_key("gone-host"))
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
async def test_dead_holder_takeover_does_not_steal_live_foreign_lock(r, monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", True)
    source = "PakistanLawSite:search_harvest:slot8"
    key = LOCK_KEY.format(source=source)
    await r.delete(key)
    host = "peer-scraper"
    live = f"{host}:42:{'a' * 16}"
    await r.set(key, live, ex=60)
    await r.set(worker_alive_redis_key(host), "1", ex=60)
    other = SessionLock(source, r, holder_identity=True)
    with pytest.raises(SessionLockHeld):
        await other.acquire(dead_holder_takeover=True)
    await r.delete(key, worker_alive_redis_key(host))


@pytest.mark.asyncio
async def test_takeover_uses_compare_and_delete_not_plain_del(r, monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", True)
    source = "PakistanLawSite:search_harvest:slot-cmp"
    key = LOCK_KEY.format(source=source)
    await r.delete(key)
    live = lock_holder_token()
    await r.set(key, live, ex=60)
    lock = SessionLock(source, r, holder_identity=True)
    assert await lock._clear_exclusive_if_value("wrong-token") is False
    assert (await r.get(key)).decode() == live
    await r.delete(key)


@pytest.mark.asyncio
async def test_dead_holder_takeover_never_applies_to_shared_lock(r, monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", True)
    source = "PakistanLawSite:shared-takeover"
    exclusive = LOCK_KEY.format(source=source)
    await r.delete(exclusive, f"{exclusive}:holders")
    await r.set(exclusive, "stale:1:deadbeefdeadbeef", ex=60)
    lock = SessionLock(source, r, max_holders=2, holder_identity=True)
    with pytest.raises(SessionLockHeld):
        await lock.acquire(dead_holder_takeover=True)
    await r.delete(exclusive, f"{exclusive}:holders")


def test_shutdown_release_ignores_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    import redis

    client = redis.from_url(settings.REDIS_URL)
    key = LOCK_KEY.format(source="PakistanLawSite:search_harvest:shutdown-token")
    token = lock_holder_token()
    client.set(key, token, ex=120)
    register_lock(key, "wrong-token")
    assert release_all_registered_sync() == 0
    assert client.get(key) is not None
    client.delete(key)
    client.close()


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


def test_worker_process_init_registers_atexit_shutdown(monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    import redis

    registered: list = []
    monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
    _install_pls_lock_shutdown_hooks()
    assert registered and registered[0] is release_all_registered_sync

    client = redis.from_url(settings.REDIS_URL)
    key = LOCK_KEY.format(source="PakistanLawSite:search_harvest:atexit-test")
    token = lock_holder_token()
    client.set(key, token, ex=120)
    register_lock(key, token)
    assert release_all_registered_sync() == 1
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


@pytest.mark.asyncio
async def test_harvest_lock_flags_off_use_legacy_token_and_no_takeover(monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", False)
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", False)
    client = aioredis.from_url(settings.REDIS_URL)
    source = "PakistanLawSite:search_harvest:slot-flags"
    key = LOCK_KEY.format(source=source)
    await client.delete(key)
    stale = f"gone-host:{999999}:deadbeefdeadbeef"
    await client.set(key, stale, ex=60)
    lock = SessionLock(source, client)
    with pytest.raises(SessionLockHeld):
        await lock.acquire(dead_holder_takeover=True)
    await client.delete(key)
    await client.aclose()


def test_compute_stalled_locks_held_no_staging_separate_field(monkeypatch):
    monkeypatch.setattr("scraper.deploy_role.harvests_login_sources", lambda: True)
    monkeypatch.setattr(settings, "PLS_STALL_LOCK_HELD_NO_STAGING_ENABLED", True)
    monkeypatch.setattr(settings, "PLS_STALL_LOCK_HELD_NO_STAGING_MINUTES", 15)
    now = datetime.now(timezone.utc)
    verdict = compute_stalled(
        last_promotion_at=now - timedelta(hours=8),
        now=now,
        threshold_hours=6,
        search_harvest_locks_held=True,
        last_staging_at=now - timedelta(minutes=20),
        search_harvest_lock_held_minutes=20.0,
    )
    assert verdict["stalled"] is True
    assert verdict["stalled_reason"] == "no_output_while_harvesting"
    assert verdict["locks_held_no_staging"] is True

    ok = compute_stalled(
        last_promotion_at=now - timedelta(minutes=10),
        now=now,
        threshold_hours=6,
        search_harvest_locks_held=True,
        last_staging_at=now - timedelta(minutes=5),
        search_harvest_lock_held_minutes=20.0,
    )
    assert ok["stalled"] is False
    assert ok.get("locks_held_no_staging") is False
