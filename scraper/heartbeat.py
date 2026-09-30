"""Heartbeats: each scheduled job stamps a Redis key when it runs, so /status can show which moving part
last did its work and when (the scheduler stopped silently for 85 minutes on 30 September 2026 and no
page said so). A stamp never fails the job that writes it."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from scraper.config import settings

logger = logging.getLogger(__name__)
_PREFIX = "corpus:heartbeat:"
_TTL_SECONDS = 7 * 24 * 3600


async def beat(name: str, *, client=None) -> None:
    """Record that `name` (dispatch, promotion, archive_mirror, embeddings, login_session, public) ran now."""
    own = client is None
    try:
        if own:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
        await client.set(_PREFIX + name, datetime.now(timezone.utc).isoformat(), ex=_TTL_SECONDS)
    except Exception as exc:  # a heartbeat is diagnostics only
        logger.debug("heartbeat %s not recorded: %s", name, exc)
    finally:
        if own and client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


async def read_all(*, client=None) -> Dict[str, Optional[str]]:
    """Every recorded heartbeat, {name: ISO time}. Empty when Redis cannot be reached."""
    own = client is None
    try:
        if own:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
        out: Dict[str, Optional[str]] = {}
        async for key in client.scan_iter(match=_PREFIX + "*", count=100):
            name = (key.decode() if isinstance(key, bytes) else key)[len(_PREFIX):]
            value = await client.get(key)
            out[name] = value.decode() if isinstance(value, bytes) else value
        return out
    except Exception as exc:
        logger.debug("heartbeats not readable: %s", exc)
        return {}
    finally:
        if own and client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
