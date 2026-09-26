#!/usr/bin/env python3
"""Host or container entrypoint for PLS slot keepalive (ClearLoginHistory-style re-login when needed).

Only slots that are not ACTIVE, or ACTIVE slots that fail a live search probe, are recovered.
Healthy ACTIVE slots are left alone.
"""

from __future__ import annotations

from scraper.database import run_async
from scraper.tasks.pls_self_healing import keepalive_pakistanlawsite_slots


def main() -> int:
    result = run_async(keepalive_pakistanlawsite_slots())
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
