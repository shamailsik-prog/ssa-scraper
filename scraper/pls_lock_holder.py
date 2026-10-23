"""Hostname+pid identity for PakistanLawSite Redis harvest locks and dead-holder detection."""

from __future__ import annotations

import os
import socket
from typing import Optional, Tuple

WORKER_ALIVE_KEY_PREFIX = "pls:worker_alive:"
WORKER_ALIVE_TTL_SECONDS = 60

_process_lock_host: Optional[str] = None


def local_hostname() -> str:
    return socket.gethostname() or "unknown"


def set_process_lock_host(host: str) -> None:
    global _process_lock_host
    host = (host or "").strip()
    if host:
        _process_lock_host = host


def lock_host_identity() -> str:
    """Host segment used in lock tokens and ``pls:worker_alive`` (must match)."""
    if _process_lock_host:
        return _process_lock_host
    return local_hostname()


def configure_lock_host_from_task_hostname(hostname: str) -> None:
    """Use the Celery worker ``name@host`` host part (``%h``) for lock/heartbeat identity."""
    full = (hostname or "").strip()
    if "@" in full:
        set_process_lock_host(full.split("@", 1)[1])
    elif full:
        set_process_lock_host(full)


def worker_alive_redis_key(host: str) -> str:
    return f"{WORKER_ALIVE_KEY_PREFIX}{host}"


def lock_holder_token() -> str:
    """Redis lock value: ``host:pid:nonce`` (nonce keeps compare-and-delete semantics)."""
    import secrets

    return f"{lock_host_identity()}:{os.getpid()}:{secrets.token_hex(8)}"


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
        # Permission denied or other ambiguous errors are not proof of death.
        return True
    return True


async def touch_worker_alive(redis_client) -> None:
    await redis_client.set(worker_alive_redis_key(lock_host_identity()), "1", ex=WORKER_ALIVE_TTL_SECONDS)


async def remote_worker_heartbeat_present(redis_client, host: str) -> bool:
    if host == lock_host_identity():
        return True
    return bool(await redis_client.exists(worker_alive_redis_key(host)))


async def lock_value_holder_stale(redis_client, value: str) -> bool:
    """Only a definitively missing local process proves a stale lock."""
    parsed = parse_lock_holder(value)
    if parsed is None:
        return False
    host, pid, _ = parsed
    if host == lock_host_identity():
        return not local_pid_alive(pid)
    # A heartbeat can expire while a remote worker is still harvesting.
    # Missing heartbeat alone must not authorize stealing its live Redis lock.
    return False
