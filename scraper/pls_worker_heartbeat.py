"""Background worker liveness for cross-host PLS lock takeover (refreshed between page charges)."""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_daemon: Optional[threading.Thread] = None
_stop = threading.Event()
_heartbeat_host_override: Optional[str] = None


def start_worker_heartbeat_daemon(*, heartbeat_host: Optional[str] = None) -> None:
    """Touch ``pls:worker_alive:<host>`` on a fixed interval while the worker process runs."""
    from scraper.config import settings

    if not settings.PLS_LOCK_HOLDER_IDENTITY:
        return
    global _daemon, _heartbeat_host_override
    _heartbeat_host_override = heartbeat_host
    if _daemon is not None and _daemon.is_alive():
        return

    def _loop() -> None:
        import redis

        from scraper.config import settings as cfg
        from scraper.pls_lock_holder import WORKER_ALIVE_TTL_SECONDS, lock_host_identity, worker_alive_redis_key

        interval = max(5, int(getattr(cfg, "PLS_WORKER_HEARTBEAT_INTERVAL_SECONDS", 15) or 15))
        client = redis.from_url(cfg.REDIS_URL)
        try:
            while True:
                try:
                    host = _heartbeat_host_override or lock_host_identity()
                    client.set(worker_alive_redis_key(host), "1", ex=WORKER_ALIVE_TTL_SECONDS)
                except Exception as exc:
                    logger.debug("pls worker heartbeat touch failed: %s", exc)
                if _stop.wait(interval):
                    break
        finally:
            try:
                client.close()
            except Exception:
                pass

    _stop.clear()
    _daemon = threading.Thread(target=_loop, name="pls-worker-heartbeat", daemon=True)
    _daemon.start()


def stop_worker_heartbeat_daemon() -> None:
    global _daemon, _heartbeat_host_override
    _stop.set()
    if _daemon is not None and _daemon.is_alive():
        _daemon.join(timeout=3)
    _daemon = None
    _heartbeat_host_override = None
    _stop.clear()
