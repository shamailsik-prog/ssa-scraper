"""Hostname+pid identity for PakistanLawSite Redis harvest locks and dead-holder detection."""

from __future__ import annotations

import os
import socket
from typing import Optional, Tuple

WORKER_ALIVE_KEY_PREFIX = "pls:worker_alive:"
WORKER_ALIVE_TTL_SECONDS = 60


def local_hostname() -> str:
    return socket.gethostname() or "unknown"


def worker_alive_redis_key(host: str) -> str:
    return f"{WORKER_ALIVE_KEY_PREFIX}{host}"


def lock_holder_token() -> str:
    """Redis lock value: ``host:pid:nonce`` (nonce keeps compare-and-delete semantics)."""
    import secrets

    return f"{local_hostname()}:{os.getpid()}:{secrets.token_hex(8)}"


def parse_lock_holder(value: str) -> Optional[Tuple[str, int, str]]:
    if not value or ":" not in value:
        return None
    parts = value.split(":", 2)
    if len(parts) != 3:
        return None
    host, pid_s, nonce = parts[0], parts[1], parts[2]
    try:
        pid = int(pid_s)
    except ValueError:
        return None
    if not host or not nonce:
        return None
    return host, pid, nonce


def local_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM and other ambiguous errors are not evidence that a worker died.
        return True
    return True


async def touch_worker_alive(redis_client) -> None:
    await redis_client.set(worker_alive_redis_key(local_hostname()), "1", ex=WORKER_ALIVE_TTL_SECONDS)


async def remote_worker_heartbeat_present(redis_client, host: str) -> bool:
    if host == local_hostname():
        return True
    return bool(await redis_client.exists(worker_alive_redis_key(host)))


async def lock_value_holder_stale(redis_client, value: str) -> bool:
    """Only a definitively missing local process proves a stale lock."""
    parsed = parse_lock_holder(value)
    if parsed is None:
        return False
    host, pid, _ = parsed
    if host == local_hostname():
        return not local_pid_alive(pid)
    # A remote heartbeat can expire during a slow page while its Redis lock is still live.
    # Missing heartbeat alone is not proof of death: fail closed until lock TTL expires.
    return False
