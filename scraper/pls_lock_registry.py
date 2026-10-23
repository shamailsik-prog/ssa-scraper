"""Track PLS Redis locks held by this worker so shutdown can release them."""

from __future__ import annotations

import logging
import signal
import threading
from typing import List, Set, Tuple

logger = logging.getLogger(__name__)

_held: List[Tuple[str, str]] = []
_guard = threading.Lock()


def register_lock(redis_key: str, token: str) -> None:
    with _guard:
        _held.append((redis_key, token))


def unregister_lock(redis_key: str, token: str) -> None:
    with _guard:
        try:
            _held.remove((redis_key, token))
        except ValueError:
            pass


def _signals_to_block() -> Set[int]:
    sigs: Set[int] = {signal.SIGTERM}
    if hasattr(signal, "SIGINT"):
        sigs.add(signal.SIGINT)
    return sigs


def release_all_registered_sync() -> int:
    """Best-effort compare-and-delete for every lock this process still tracks."""
    from scraper.config import settings

    if not settings.PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN:
        return 0

    prior_mask = None
    try:
        if hasattr(signal, "pthread_sigmask"):
            try:
                prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _signals_to_block())
            except (ValueError, OSError, AttributeError):
                prior_mask = None

        with _guard:
            pairs = list(_held)
            _held.clear()

        if not pairs:
            return 0
        try:
            import redis
        except ImportError:
            return 0
        released = 0
        try:
            client = redis.from_url(settings.REDIS_URL)
            for key, token in pairs:
                try:
                    ok = client.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                        1,
                        key,
                        token,
                    )
                    if int(ok or 0) == 1:
                        released += 1
                except Exception as exc:
                    logger.warning("pls lock shutdown: could not release %s: %s", key, exc)
            client.close()
        except Exception as exc:
            logger.warning("pls lock shutdown: redis unavailable: %s", exc)
        if released:
            logger.info("pls lock shutdown: released %s registered lock(s)", released)
        return released
    finally:
        if prior_mask is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
            except (ValueError, OSError, AttributeError):
                pass
