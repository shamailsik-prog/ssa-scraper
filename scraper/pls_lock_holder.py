"""Hostname+pid identity for PakistanLawSite Redis harvest locks and dead-holder detection."""

from __future__ import annotations

import os
import socket
from typing import Optional, Tuple


def local_hostname() -> str:
    return socket.gethostname() or "unknown"


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


def holder_process_alive(host: str, pid: int, *, local_host: Optional[str] = None) -> bool:
    """False when the holder is certainly gone (dead pid on this host, or a different host)."""
    local = local_host or local_hostname()
    if host != local:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def lock_value_holder_dead(value: str, *, local_host: Optional[str] = None) -> bool:
    parsed = parse_lock_holder(value)
    if parsed is None:
        return False
    host, pid, _ = parsed
    return not holder_process_alive(host, pid, local_host=local_host)
