"""Prune dead members from the shared login-session holders sorted set."""

from __future__ import annotations

from typing import Union


async def prune_stale_shared_holders(redis_client, exclusive_key: str) -> int:
    shared_key = f"{exclusive_key}:holders"
    from scraper.pls_lock_holder import lock_value_holder_stale

    members = await redis_client.zrange(shared_key, 0, -1)
    removed = 0
    for raw in members or []:
        token = raw.decode() if isinstance(raw, bytes) else str(raw)
        if not await lock_value_holder_stale(redis_client, token):
            continue
        if await redis_client.zrem(shared_key, raw):
            removed += 1
    if removed and int(await redis_client.zcard(shared_key) or 0) == 0:
        await redis_client.delete(shared_key)
    return removed


def prune_stale_shared_holders_sync(redis_client, exclusive_key: str) -> int:
    shared_key = f"{exclusive_key}:holders"
    from scraper.pls_lock_holder import lock_value_holder_stale_sync

    members = redis_client.zrange(shared_key, 0, -1)
    removed = 0
    for raw in members or []:
        token = raw.decode() if isinstance(raw, bytes) else str(raw)
        if not lock_value_holder_stale_sync(redis_client, token):
            continue
        if redis_client.zrem(shared_key, raw):
            removed += 1
    if removed and int(redis_client.zcard(shared_key) or 0) == 0:
        redis_client.delete(shared_key)
    return removed
