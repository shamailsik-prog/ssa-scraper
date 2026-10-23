"""Release tracked PLS harvest locks on Celery worker_process_shutdown (billiard on_process_exit)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def release_on_worker_process_shutdown() -> None:
    from scraper.config import settings

    if not settings.PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN:
        return
    from scraper.pls_lock_registry import release_all_registered_sync

    release_all_registered_sync()
