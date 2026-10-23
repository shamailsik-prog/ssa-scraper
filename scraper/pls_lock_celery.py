"""Celery helpers for PLS Redis lock takeover safety."""

from __future__ import annotations

import logging
from typing import FrozenSet

logger = logging.getLogger(__name__)

PLS_LOCK_BEARING_TASKS: FrozenSet[str] = frozenset(
    {
        "scraper.tasks.pls_search_harvest.pls_search_harvest_tick",
        "scraper.tasks.pls_caseid_walk.pls_caseid_walk_tick",
        "scraper.tasks.dispatcher.run_login_session_job",
        "scraper.tasks.login_recovery.recover_login_slots",
        "scraper.tasks.pls_self_healing.pls_keepalive_hourly",
        "scraper.tasks.pls_self_healing.pls_stall_watchdog",
        "scraper.tasks.spot_check.spot_check_judgments",
    }
)

PLS_HEARTBEAT_TASKS: FrozenSet[str] = PLS_LOCK_BEARING_TASKS


def celery_pls_lock_task_active() -> bool:
    """True when a worker is actively running a task that may hold a PLS session lock."""
    try:
        from scraper.tasks.celery_app import app

        insp = app.control.inspect(timeout=0.35)
        if insp is None:
            return False
        active = insp.active() or {}
    except Exception as exc:
        logger.debug("pls lock celery inspect failed: %s", exc)
        return False
    for tasks in active.values():
        for entry in tasks:
            if entry.get("name") in PLS_LOCK_BEARING_TASKS:
                return True
    return False
