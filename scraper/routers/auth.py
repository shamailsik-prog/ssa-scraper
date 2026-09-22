"""Admin authentication dependency. Every /admin route requires X-API-Key = ADMIN_API_KEY."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict

from fastapi import Header, HTTPException, Request, WebSocket

from scraper.config import settings

_AUTH_FAILS: dict[str, list[float]] = defaultdict(list)
_AUTH_WINDOW_SECONDS = 300
_AUTH_FAIL_LIMIT = 20


def _client_ip(request: Request | None) -> str:
    if request is None:
        return "unknown"
    return (request.client.host if request.client else None) or "unknown"


def _auth_failures(ip: str) -> int:
    now = time.time()
    kept = [stamp for stamp in _AUTH_FAILS.get(ip, []) if now - stamp < _AUTH_WINDOW_SECONDS]
    if kept:
        _AUTH_FAILS[ip] = kept
    else:
        _AUTH_FAILS.pop(ip, None)
    return len(kept)


def _record_auth_failure(ip: str) -> None:
    _AUTH_FAILS[ip].append(time.time())


def _ok(key: str | None) -> bool:
    return bool(settings.ADMIN_API_KEY) and key is not None and hmac.compare_digest(key, settings.ADMIN_API_KEY)


def require_admin(request: Request, x_api_key: str | None = Header(default=None)) -> str:
    ip = _client_ip(request)
    if _auth_failures(ip) >= _AUTH_FAIL_LIMIT:
        raise HTTPException(status_code=429, detail="Too many invalid admin-key attempts")
    if not _ok(x_api_key):
        _record_auth_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid key")
    return "admin"


def check_key(x_api_key: str | None) -> None:
    if not _ok(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid key")


async def require_admin_ws(websocket: WebSocket) -> bool:
    """WebSocket admin check. Browsers cannot set headers on a WebSocket, so the dashboard passes the
    key as ?key=; non-browser clients may send X-API-Key instead. Called directly (not as a FastAPI
    dependency), so the parameters are read from the socket, never from Query() defaults."""
    key = websocket.query_params.get("key") or websocket.headers.get("x-api-key")
    if not _ok(key):
        await websocket.close(code=4401)
        return False
    return True
