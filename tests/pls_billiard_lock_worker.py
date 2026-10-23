"""Billiard pool child entrypoint for PLS lock shutdown tests (importable module path)."""

from __future__ import annotations


def hold_registered_lock_until_killed(
    redis_url: str,
    key: str,
    token: str,
    foreign_key: str,
    foreign_val: str,
    ready_key: str,
    stop_key: str | None = None,
) -> None:
    import os
    import time

    import redis

    from scraper.config import settings
    from scraper.pls_lock_registry import register_lock
    from scraper.pls_lock_shutdown import install_pls_lock_shutdown_hooks, release_on_worker_process_shutdown

    # Parent pytest monkeypatch does not apply in forked pool children.
    settings.PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN = True

    install_pls_lock_shutdown_hooks()
    client = redis.from_url(redis_url)
    client.set(key, token, ex=600)
    client.set(foreign_key, foreign_val, ex=600)
    register_lock(key, token)
    client.set(ready_key, str(os.getpid()), ex=60)
    try:
        while True:
            if stop_key and client.get(stop_key):
                break
            time.sleep(0.2)
    finally:
        release_on_worker_process_shutdown()

