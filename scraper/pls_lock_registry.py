"""Track PLS Redis locks held by this worker so shutdown can release them."""

from __future__ import annotations

import logging
import signal
import threading
from typing import List, Literal, Optional, Set, Tuple

logger = logging.getLogger(__name__)

LockKind = Literal["exclusive", "shared"]

_held: List[Tuple[str, str, LockKind]] = []
_guard = threading.Lock()
_RELEASE_REDIS_SOCKET_TIMEOUT = 2.0


def register_lock(redis_key: str, token: str, *, kind: LockKind = "exclusive") -> None:
    with _guard:
        _held.append((redis_key, token, kind))


def unregister_lock(redis_key: str, token: str, *, kind: LockKind = "exclusive") -> None:
    with _guard:
        try:
            _held.remove((redis_key, token, kind))
        except ValueError:
            pass


def _signals_to_block() -> Set[int]:
    sigs: Set[int] = {signal.SIGTERM}
    if hasattr(signal, "SIGINT"):
        sigs.add(signal.SIGINT)
    return sigs


def _restore_signal_handlers(previous: List[Tuple[int, object]]) -> None:
    for sig, handler in previous:
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            pass


def _release_one(client, key: str, token: str, kind: LockKind) -> bool:
    try:
        if kind == "shared":
            ok = client.eval(
                """
                local removed = redis.call('zrem', KEYS[1], ARGV[1])
                if tonumber(removed) == 0 then return 0 end
                if redis.call('zcard', KEYS[1]) == 0 then redis.call('del', KEYS[1]) end
                return 1
                """,
                1,
                key,
                token,
            )
            return int(ok or 0) == 1
        ok = client.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
            1,
            key,
            token,
        )
        return int(ok or 0) == 1
    except SystemExit:
        raise
    except Exception as exc:
        logger.warning("pls lock shutdown: could not release %s: %s", key, exc)
        return False


def release_all_registered_sync() -> int:
    """Best-effort compare-and-delete for every lock this process still tracks."""
    from scraper.config import settings

    if not settings.PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN:
        return 0

    ignored: List[Tuple[int, object]] = []
    on_main = threading.current_thread() is threading.main_thread()
    if on_main:
        if hasattr(signal, "SIGTERM"):
            ignored.append((signal.SIGTERM, signal.signal(signal.SIGTERM, signal.SIG_IGN)))
        if hasattr(signal, "SIGINT"):
            ignored.append((signal.SIGINT, signal.signal(signal.SIGINT, signal.SIG_IGN)))

    prior_mask = None
    pending_exit: Optional[BaseException] = None
    try:
        if hasattr(signal, "pthread_sigmask"):
            try:
                prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _signals_to_block())
            except (ValueError, OSError, AttributeError):
                prior_mask = None

        with _guard:
            triples = list(_held)
            _held.clear()

        if not triples:
            return 0
        try:
            import redis
        except ImportError:
            return 0
        released = 0
        try:
            client = redis.from_url(settings.REDIS_URL, socket_timeout=_RELEASE_REDIS_SOCKET_TIMEOUT)
            for key, token, kind in triples:
                try:
                    if _release_one(client, key, token, kind):
                        released += 1
                except SystemExit as exc:
                    pending_exit = exc
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
        _restore_signal_handlers(ignored)
        if pending_exit is not None:
            raise pending_exit
