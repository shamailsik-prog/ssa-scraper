"""Billiard prefork child shutdown hook (production): ignore SIGTERM during lock release."""

from __future__ import annotations


def billiard_worker_process_exit(pid: int, exitcode: int) -> None:
    import signal

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    from scraper.pls_lock_shutdown import release_on_worker_process_shutdown

    release_on_worker_process_shutdown()
