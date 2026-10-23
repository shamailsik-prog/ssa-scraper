"""Celery task names for PLS worker heartbeat (lock-holding tasks)."""

from __future__ import annotations

from typing import FrozenSet

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
