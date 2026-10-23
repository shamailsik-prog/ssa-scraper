"""Billiard pool child entrypoints mirroring Celery prefork (initializer + on_process_exit)."""

from __future__ import annotations


def billiard_pool_worker_initializer() -> None:
    from scraper.config import settings
    from scraper.pls_worker_heartbeat import start_worker_heartbeat_daemon

    settings.PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN = True
    settings.PLS_LOCK_HOLDER_IDENTITY = True
    start_worker_heartbeat_daemon()


def billiard_pool_process_destructor(pid: int, exitcode: int) -> None:
    from scraper.pls_lock_shutdown import release_on_worker_process_shutdown

    release_on_worker_process_shutdown()


def hold_registered_lock_in_pool_child(
    redis_url: str,
    key: str,
    token: str,
    foreign_key: str,
    foreign_val: str,
    ready_key: str,
    stop_key: str | None = None,
) -> None:
    """Hold a registered lock until stop_key is set or the pool worker is terminated (no task-finally release)."""
    import os
    import time

    import redis

    from scraper.pls_lock_registry import register_lock

    client = redis.from_url(redis_url)
    client.set(key, token, ex=600)
    client.set(foreign_key, foreign_val, ex=600)
    register_lock(key, token)
    client.set(ready_key, str(os.getpid()), ex=60)
    while True:
        if stop_key and client.get(stop_key):
            return
        time.sleep(0.2)
