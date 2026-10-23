"""PLS search-harvest Redis lock resilience (orphaned locks after worker recreate)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from billiard.pool import Pool as BilliardPool
import pytest
import redis
import redis.asyncio as aioredis

from scraper.auth.session_manager import LOCK_KEY, SessionLock, SessionLockHeld
from scraper.config import settings
from scraper.pls_grid_health import compute_stalled, release_pls_search_harvest_redis_locks
from scraper.pls_lock_holder import (
    local_hostname,
    lock_holder_token,
    lock_value_holder_stale,
    parse_lock_holder,
    worker_alive_redis_key,
)
from scraper.pls_lock_registry import register_lock, release_all_registered_sync
from scraper.pls_lock_shutdown import release_on_worker_process_shutdown
from tests.pls_billiard_lock_worker import (
    billiard_pool_process_destructor,
    billiard_pool_worker_initializer,
    hold_registered_lock_in_pool_child,
)


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


def test_shutdown_release_redis_client_uses_socket_timeout(monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    import redis as redis_sync

    captured: list[dict] = []
    real_from_url = redis_sync.from_url

    def _from_url(url, **kwargs):
        captured.append(kwargs)
        return real_from_url(url, **kwargs)

    monkeypatch.setattr(redis_sync, "from_url", _from_url)
    key = LOCK_KEY.format(source="PakistanLawSite:search_harvest:socket-timeout")
    token = lock_holder_token()
    client = redis_sync.from_url(settings.REDIS_URL)
    client.set(key, token, ex=120)
    register_lock(key, token)
    release_all_registered_sync()
    assert client.get(key) is None
    client.close()
    assert any(c.get("socket_timeout") == 2.0 for c in captured)


def test_release_all_registered_sync_finishes_remaining_locks_after_system_exit(monkeypatch):
    """Pending SIGTERM/SystemExit during eval must not skip later lock releases."""
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    import redis as redis_sync

    key1 = LOCK_KEY.format(source="PakistanLawSite:search_harvest:exit-a")
    key2 = LOCK_KEY.format(source="PakistanLawSite:search_harvest:exit-b")
    token1 = lock_holder_token()
    token2 = lock_holder_token()
    client = redis_sync.from_url(settings.REDIS_URL)
    client.set(key1, token1, ex=120)
    client.set(key2, token2, ex=120)
    register_lock(key1, token1)
    register_lock(key2, token2)

    real_from_url = redis_sync.from_url
    calls = 0

    class _Client:
        def __init__(self, inner):
            self._inner = inner

        def eval(self, script, numkeys, key, token):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SystemExit(143)
            return self._inner.eval(script, numkeys, key, token)

        def close(self):
            self._inner.close()

    def _from_url(url, **kwargs):
        return _Client(real_from_url(url, **kwargs))

    monkeypatch.setattr(redis_sync, "from_url", _from_url)
    with pytest.raises(SystemExit):
        release_all_registered_sync()
    assert client.get(key1) == token1.encode()
    assert client.get(key2) is None
    client.delete(key1)
    client.close()


def test_release_all_registered_sync_blocks_until_guard_available(monkeypatch):
    """Mutant S1: release must hold sigmask through Redis DEL (blocking guard, not fail-open skip)."""
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    import redis
    import threading

    client = redis.from_url(settings.REDIS_URL)
    key = LOCK_KEY.format(source="PakistanLawSite:search_harvest:reentrant")
    token = lock_holder_token()
    client.set(key, token, ex=120)
    register_lock(key, token)
    from scraper.pls_lock_registry import _guard

    released: list[int] = []

    def _second_release() -> None:
        released.append(release_all_registered_sync())

    _guard.acquire()
    try:
        t = threading.Thread(target=_second_release)
        t.start()
        time.sleep(0.15)
        assert released == []
        _guard.release()
        t.join(timeout=5)
    finally:
        if _guard.locked():
            _guard.release()
    assert released == [1]
    assert client.get(key) is None
    client.close()


@pytest.mark.asyncio
async def test_acquire_with_holder_identity_refreshes_worker_alive(r, monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    host = local_hostname()
    await r.delete(worker_alive_redis_key(host))
    source = "PakistanLawSite:search_harvest:slot-touch"
    key = LOCK_KEY.format(source=source)
    await r.delete(key)
    lock = SessionLock(source, r, holder_identity=True)
    await lock.acquire()
    try:
        assert await r.exists(worker_alive_redis_key(host))
    finally:
        await lock.release()
    await r.delete(key)


def _wait_redis_key(client: redis.Redis, key: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(key):
            return True
        time.sleep(0.05)
    return False


def _billiard_pool_release_scenario(monkeypatch, *, use_close_join: bool) -> None:
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    uid = uuid.uuid4().hex[:10]
    key = f"corpus:login_session_lock:PakistanLawSite:search_harvest:billiard-{uid}"
    foreign = f"corpus:login_session_lock:PakistanLawSite:search_harvest:foreign-{uid}"
    ready_key = f"pls:billiard-test-ready-{uid}"
    stop_key = f"pls:billiard-test-stop-{uid}"
    token = lock_holder_token()
    foreign_val = lock_holder_token()
    client = redis.from_url(settings.REDIS_URL)
    client.delete(key, foreign, ready_key, stop_key)
    pool = BilliardPool(
        1,
        initializer=billiard_pool_worker_initializer,
        on_process_exit=billiard_pool_process_destructor,
    )
    try:
        pool.apply_async(
            hold_registered_lock_in_pool_child,
            (settings.REDIS_URL, key, token, foreign, foreign_val, ready_key, stop_key if use_close_join else None),
        )
        assert _wait_redis_key(client, ready_key)
        if use_close_join:
            client.set(stop_key, "1", ex=60)
            pool.close()
            pool.join()
        else:
            pool.terminate()
            pool.join()
        for _ in range(40):
            if client.get(key) is None:
                break
            time.sleep(0.1)
    finally:
        try:
            pool.terminate()
        except Exception:
            pass
        pool.join()
    assert client.get(key) is None
    assert client.get(foreign) is not None
    client.delete(foreign, ready_key, stop_key)
    client.close()


def test_billiard_pool_terminate_releases_only_registered_lock(monkeypatch):
    _billiard_pool_release_scenario(monkeypatch, use_close_join=False)


def test_billiard_pool_sigterm_mid_task_releases_only_registered_lock(monkeypatch):
    """on_process_exit release must run when the child gets SIGTERM during the hold loop."""
    import os
    import signal

    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    uid = uuid.uuid4().hex[:10]
    key = f"corpus:login_session_lock:PakistanLawSite:search_harvest:billiard-sig-{uid}"
    foreign = f"corpus:login_session_lock:PakistanLawSite:search_harvest:foreign-sig-{uid}"
    ready_key = f"pls:billiard-sig-ready-{uid}"
    token = lock_holder_token()
    foreign_val = lock_holder_token()
    client = redis.from_url(settings.REDIS_URL)
    client.delete(key, foreign, ready_key)
    pool = BilliardPool(
        1,
        initializer=billiard_pool_worker_initializer,
        on_process_exit=billiard_pool_process_destructor,
    )
    try:
        pool.apply_async(
            hold_registered_lock_in_pool_child,
            (settings.REDIS_URL, key, token, foreign, foreign_val, ready_key, None),
        )
        assert _wait_redis_key(client, ready_key)
        child_pid = int(client.get(ready_key).decode())
        os.kill(child_pid, signal.SIGTERM)
        for _ in range(40):
            if client.get(key) is None:
                break
            time.sleep(0.1)
    finally:
        try:
            pool.terminate()
        except Exception:
            pass
        pool.join()
    assert client.get(key) is None
    assert client.get(foreign) is not None
    client.delete(foreign, ready_key)
    client.close()


def test_billiard_pool_close_join_releases_only_registered_lock(monkeypatch):
    _billiard_pool_release_scenario(monkeypatch, use_close_join=True)


def test_billiard_initializer_starts_worker_heartbeat(monkeypatch):
    """Mutant H6: heartbeat daemon must start in pool initializer (not only on lock acquire)."""
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    uid = uuid.uuid4().hex[:10]
    ready_key = f"pls:billiard-hb-ready-{uid}"
    client = redis.from_url(settings.REDIS_URL)
    for k in client.scan_iter("pls:worker_alive:*"):
        client.delete(k)
    pool = BilliardPool(
        1,
        initializer=billiard_pool_worker_initializer,
        on_process_exit=billiard_pool_process_destructor,
    )
    try:
        pool.apply_async(
            hold_registered_lock_in_pool_child,
            (settings.REDIS_URL, "k", "t", "fk", "fv", ready_key, None),
        )
        assert _wait_redis_key(client, ready_key)
        assert list(client.scan_iter("pls:worker_alive:*"))
        pool.terminate()
        pool.join()
    finally:
        try:
            pool.terminate()
        except Exception:
            pass
        pool.join()
        client.delete(ready_key)
        for k in client.scan_iter("pls:worker_alive:*"):
            client.delete(k)
        client.close()


def test_harvest_tick_finally_releases_registered_locks(monkeypatch):
    """Mutant H7: pls_search_harvest_tick must release tracked locks in finally."""
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    calls: list[str] = []

    def _track_release() -> None:
        calls.append("release")
        release_on_worker_process_shutdown()

    monkeypatch.setattr("scraper.pls_lock_shutdown.release_on_worker_process_shutdown", _track_release)
    monkeypatch.setattr("scraper.tasks.deploy_hold.login_work_held", lambda: True)
    from scraper.tasks import pls_search_harvest

    pls_search_harvest.pls_search_harvest_tick()
    assert calls == ["release"]


def test_worker_process_shutdown_handler_releases(monkeypatch):
    """Mutant H8: worker_process_shutdown path must call release_all_registered_sync."""
    monkeypatch.setattr(settings, "PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN", True)
    import redis

    client = redis.from_url(settings.REDIS_URL)
    key = LOCK_KEY.format(source="PakistanLawSite:search_harvest:shutdown-hook")
    token = lock_holder_token()
    client.set(key, token, ex=120)
    register_lock(key, token)
    release_on_worker_process_shutdown()
    assert client.get(key) is None
    client.close()


@pytest.mark.asyncio
async def test_heartbeat_daemon_prevents_takeover_during_long_idle(r, monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", True)
    monkeypatch.setattr("scraper.pls_lock_holder.WORKER_ALIVE_TTL_SECONDS", 60)
    monkeypatch.setattr(settings, "PLS_WORKER_HEARTBEAT_INTERVAL_SECONDS", 15)
    peer_host = "holder-daemon-host"
    from scraper.pls_worker_heartbeat import start_worker_heartbeat_daemon, stop_worker_heartbeat_daemon

    source = "PakistanLawSite:search_harvest:slot-daemon"
    key = LOCK_KEY.format(source=source)
    await r.delete(key, worker_alive_redis_key(peer_host))
    stop_worker_heartbeat_daemon()
    start_worker_heartbeat_daemon(heartbeat_host=peer_host)
    try:
        live = f"{peer_host}:999:{'b' * 16}"
        await r.set(key, live, ex=120)
        await asyncio.sleep(1)
        assert await r.exists(worker_alive_redis_key(peer_host))
        # Lock holder is idle (no page refresh); daemon must keep heartbeat past 60s TTL.
        await asyncio.sleep(65)
        assert await r.exists(worker_alive_redis_key(peer_host))
        other = SessionLock(source, r, holder_identity=True)
        with pytest.raises(SessionLockHeld):
            await other.acquire(dead_holder_takeover=True)
    finally:
        stop_worker_heartbeat_daemon()
        await r.delete(key, worker_alive_redis_key(peer_host))


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
async def test_dead_holder_takeover_never_steals_legacy_sha_lock_value(r, monkeypatch):
    """Legacy pre-identity tokens are not taken over; wait for TTL or deploy release."""
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    monkeypatch.setattr(settings, "PLS_LOCK_DEAD_HOLDER_TAKEOVER", True)
    source = "PakistanLawSite:search_harvest:slot-legacy"
    key = LOCK_KEY.format(source=source)
    legacy = "a" * 64
    await r.set(key, legacy, ex=120)
    lock = SessionLock(source, r, holder_identity=True)
    with pytest.raises(SessionLockHeld):
        await lock.acquire(dead_holder_takeover=True)
    await r.delete(key)


def test_pls_lock_task_prerun_starts_heartbeat_for_harvest_task(monkeypatch):
    monkeypatch.setattr(settings, "PLS_LOCK_HOLDER_IDENTITY", True)
    from scraper.pls_worker_heartbeat import stop_worker_heartbeat_daemon
    import scraper.pls_worker_heartbeat as wh

    stop_worker_heartbeat_daemon()
    from scraper.tasks.celery_app import _pls_lock_task_prerun
    from scraper.tasks.pls_search_harvest import pls_search_harvest_tick

    class _Req:
        hostname = "scraper-public@worker-public-host"

    class _Task:
        request = _Req()

    _pls_lock_task_prerun(sender=pls_search_harvest_tick, task=_Task())
    assert wh._daemon is not None and wh._daemon.is_alive()
    from scraper.pls_lock_holder import lock_host_identity

    assert lock_host_identity() == "worker-public-host"
    stop_worker_heartbeat_daemon()


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

    young = compute_stalled(
        last_promotion_at=now - timedelta(hours=8),
        now=now,
        threshold_hours=6,
        search_harvest_locks_held=True,
        last_staging_at=now - timedelta(minutes=20),
        search_harvest_lock_held_minutes=5.0,
    )
    assert young["locks_held_no_staging"] is False
