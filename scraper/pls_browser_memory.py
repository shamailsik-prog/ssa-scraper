"""Process RSS guards for long PakistanLawSite Playwright harvest loops."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from scraper.config import settings

logger = logging.getLogger(__name__)


class PlsBrowserMemoryHardLimit(RuntimeError):
    """Worker RSS exceeded PLS_BROWSER_RSS_HARD_MB; stop cleanly before OOM."""


@dataclass
class PlsBrowserMemoryState:
    windows_since_recycle: int = 0

    def note_window(self) -> None:
        self.windows_since_recycle += 1

    def reset_recycle_counter(self) -> None:
        self.windows_since_recycle = 0


def current_rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb / 1024.0
    except OSError:
        return None
    return None


def check_rss_limits(state: PlsBrowserMemoryState) -> tuple[bool, Optional[str]]:
    """Return (should_recycle_browser, stop_reason).

    stop_reason is set when the hard limit is exceeded (caller must halt the job).
    """
    rss = current_rss_mb()
    hard = float(getattr(settings, "PLS_BROWSER_RSS_HARD_MB", 0) or 0)
    soft = float(getattr(settings, "PLS_BROWSER_RSS_LIMIT_MB", 0) or 0)
    if rss is not None and hard > 0 and rss >= hard:
        return False, f"RSS {rss:.0f} MiB >= hard limit {hard:.0f} MiB"
    recycle_windows = int(getattr(settings, "PLS_BROWSER_RECYCLE_WINDOWS", 0) or 0)
    if recycle_windows > 0 and state.windows_since_recycle >= recycle_windows:
        return True, None
    if rss is not None and soft > 0 and rss >= soft:
        return True, None
    return False, None


def apply_memory_guard(state: PlsBrowserMemoryState) -> None:
    should_recycle, stop_reason = check_rss_limits(state)
    if stop_reason:
        raise PlsBrowserMemoryHardLimit(stop_reason)
    if should_recycle:
        state.reset_recycle_counter()
