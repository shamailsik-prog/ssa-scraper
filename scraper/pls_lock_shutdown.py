"""Process-level hooks to release tracked PLS harvest locks (billiard workers skip atexit)."""

from __future__ import annotations

import logging
import signal
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_prev_handlers: Dict[int, Any] = {}


def install_pls_lock_shutdown_hooks() -> None:
    from scraper.config import settings

    if not settings.PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN:
        return
    from scraper.pls_lock_registry import release_all_registered_sync

    def _release_and_chain(signum: int, frame) -> None:
        try:
            release_all_registered_sync()
        except Exception as exc:
            logger.warning("pls lock signal handler: release failed: %s", exc)
        prev = _prev_handlers.get(signum)
        if prev is signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)
        elif prev is signal.SIG_IGN:
            return
        elif callable(prev):
            prev(signum, frame)
        elif signum == signal.SIGTERM:
            raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        _prev_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, _release_and_chain)


def release_on_worker_process_shutdown() -> None:
    from scraper.config import settings

    if not settings.PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN:
        return
    from scraper.pls_lock_registry import release_all_registered_sync

    release_all_registered_sync()
