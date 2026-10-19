"""Optional harvest lock hygiene (feature-flagged).

Clears stale Redis locks for PakistanLawSite search harvest on worker start, and refreshes held
locks during a long tick so a deploy cannot leave slot/query locks until LOCK_TTL_SECONDS expires.
When the flags are off, every function is a no-op and existing behaviour is unchanged.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from scraper.auth.session_manager import SessionLock

logger = logging.getLogger(__name__)

SOURCE_NAME = "PakistanLawSite"
_LOCK_PREFIX = f"corpus:login_session_lock:{SOURCE_NAME}:search_harvest"


def harvest_lock_key_patterns() -> Sequence[str]:
    return (
        f"{_LOCK_PREFIX}:slot1",
        f"{_LOCK_PREFIX}:slot2",
        f"{_LOCK_PREFIX}:query:*",
    )


async def clear_stale_harvest_locks(redis_client=None) -> int:
    """Drop search-harvest slot and per-query locks left by a dead worker (deploy/OOM).

    Returns the number of Redis keys deleted. Safe to call when no harvest is running; when a live
    harvest holds a lock, it will re-acquire on the next refresh or fail fast on the next tick.
    """
    from scraper.config import settings

    if not settings.SSA_CLEAR_HARVEST_LOCKS_ON_START:
        return 0
    client = redis_client
    own = client is None
    if own:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL)
    deleted = 0
    try:
        for pattern in harvest_lock_key_patterns():
            if pattern.endswith("*"):
                cursor = 0
                while True:
                    cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
                    if keys:
                        deleted += int(await client.delete(*keys) or 0)
                    if cursor == 0:
                        break
            else:
                deleted += int(await client.delete(pattern) or 0)
        if deleted:
            logger.warning("Cleared %s stale PakistanLawSite search-harvest lock key(s) at worker start", deleted)
    finally:
        if own:
            try:
                await client.aclose()
            except Exception:
                pass
    return deleted


async def refresh_harvest_locks(locks: Iterable[Optional[SessionLock]]) -> None:
    """Extend TTL on harvest-held SessionLocks when SSA_HARVEST_LOCK_HEARTBEAT is enabled."""
    from scraper.config import settings

    if not settings.SSA_HARVEST_LOCK_HEARTBEAT:
        return
    for lock in locks:
        if lock is None:
            continue
        try:
            await lock.refresh()
        except Exception as exc:
            logger.debug("harvest lock refresh skipped: %s", exc)
