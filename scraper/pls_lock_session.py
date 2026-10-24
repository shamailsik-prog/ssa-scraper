"""Shared kwargs for PakistanLawSite Redis session locks (identity + shutdown tracking)."""

from __future__ import annotations

from typing import Any, Dict


def pls_session_lock_kwargs() -> Dict[str, Any]:
    from scraper.config import settings

    return {
        "holder_identity": bool(settings.PLS_LOCK_HOLDER_IDENTITY),
        "track_shutdown": bool(settings.PLS_LOCK_RELEASE_ON_WORKER_SHUTDOWN),
    }
