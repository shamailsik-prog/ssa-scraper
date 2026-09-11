"""Admin authentication dependency. Every /admin route requires X-API-Key = ADMIN_API_KEY."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Query, WebSocket

from scraper.config import settings


def _ok(key: str | None) -> bool:
    return bool(settings.ADMIN_API_KEY) and key is not None and hmac.compare_digest(key, settings.ADMIN_API_KEY)


def require_admin(x_api_key: str | None = Header(default=None)) -> str:
    if not _ok(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid key")
    return "admin"


def check_key(x_api_key: str | None) -> None:
    if not _ok(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid key")


async def require_admin_ws(websocket: WebSocket, key: str | None = Query(default=None)) -> bool:
    if not _ok(key):
        await websocket.close(code=4401)
        return False
    return True
