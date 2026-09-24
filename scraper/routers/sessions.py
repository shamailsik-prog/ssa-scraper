"""
/admin/sessions — login-session slots and the streamed human login (Amendment §9).

    POST  /admin/sessions/{source}/login/start   {slot}      → opens a server-side browser
    WS    /admin/sessions/{source}/login/stream?key=…        → JPEG frames out, input events in
    POST  /admin/sessions/{source}/login/complete             → verifies auth, stores encrypted state
    POST  /admin/sessions/{source}/login/cancel
    GET   /admin/sessions/{source}                            → slot states
    GET   /admin/sessions/{source}/credentials                → encrypted credential slot status
    POST  /admin/sessions/{source}/credentials/{n}            → save encrypted username/password
    POST  /admin/sessions/{source}/credentials/{n}/clear      → clear saved credentials
    POST  /admin/sessions/{source}/slots/{n}/{pause|resume|clear}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.auth.browser_login import LoginSessionError, registry
from scraper.auth.session_manager import NoActiveSlot, SessionManager
from scraper.config import settings
from scraper.database import get_db
from scraper.models import BrowserSessionSlot, ScraperSource
from scraper.routers.auth import require_admin, require_admin_ws

router = APIRouter(prefix="/admin/sessions", tags=["sessions"])


class StartLogin(BaseModel):
    slot: int = Field(default=1, ge=1, le=2)
    started_by: str = "operator"
    use_saved_credentials: bool = True
    auto_complete_if_empty: bool = True
    # Size of the streamed browser. A phone-sized viewport makes the site render its mobile layout,
    # so the stream fits a phone screen and fields are large enough to tap.
    viewport_width: Optional[int] = Field(default=None, ge=320, le=1920)
    viewport_height: Optional[int] = Field(default=None, ge=480, le=1600)


class SaveCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=300)
    saved_by: str = "operator"


async def _source(db: AsyncSession, name: str) -> ScraperSource:
    s = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == name))).scalars().first()
    if s is None or s.access_method != "login_session":
        raise HTTPException(404, "login_session source not found")
    return s


def _slot_view(r: BrowserSessionSlot) -> Dict[str, Any]:
    return {
        "slot": r.slot_number,
        "role": r.role,
        "state": r.state,
        "reason": r.state_reason,
        "logged_in_at": r.logged_in_at.isoformat() if r.logged_in_at else None,
        "logged_in_by": r.logged_in_by,
        "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        "reconnects": r.reconnect_count,
        "credentials": {
            "username": "CONFIGURED" if r.login_username_encrypted else "NOT CONFIGURED",
            "password": "CONFIGURED" if r.login_password_encrypted else "NOT CONFIGURED",
            "updated_at": r.login_credentials_updated_at.isoformat() if r.login_credentials_updated_at else None,
            "updated_by": r.login_credentials_updated_by,
        },
    }


async def _slot_or_422(manager: SessionManager, slot: int) -> BrowserSessionSlot:
    try:
        return await manager.slot(slot)
    except NoActiveSlot as exc:
        raise HTTPException(422, str(exc))


@router.get("/{source}", dependencies=[Depends(require_admin)])
async def slots(source: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    manager = SessionManager(db, s)
    rows = await manager.slots()
    return {
        "source": source,
        "state": s.state,
        "state_reason": s.state_reason,
        "login_scraping_permitted": settings.login_scraping_effective,
        "current_slot": (s.config_json or {}).get("current_slot"),
        "slots": [_slot_view(r) for r in rows],
        "open_login_sessions": registry.status(),
    }


@router.get("/{source}/credentials", dependencies=[Depends(require_admin)])
async def get_credentials(source: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    rows = await SessionManager(db, s).slots()
    return {
        "source": source,
        "slots": [
            {
                "slot": r.slot_number,
                "role": r.role,
                "username": "CONFIGURED" if r.login_username_encrypted else "NOT CONFIGURED",
                "password": "CONFIGURED" if r.login_password_encrypted else "NOT CONFIGURED",
                "updated_at": r.login_credentials_updated_at.isoformat() if r.login_credentials_updated_at else None,
                "updated_by": r.login_credentials_updated_by,
            }
            for r in rows
        ],
    }


@router.post("/{source}/credentials/{slot}", dependencies=[Depends(require_admin)])
async def save_credentials(source: str, slot: int, body: SaveCredentials, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    if not settings.ENCRYPTION_KEY:
        raise HTTPException(409, "ENCRYPTION_KEY is NOT CONFIGURED; credentials cannot be encrypted")
    username = body.username.strip()
    if not username:
        raise HTTPException(422, "username is required")
    manager = SessionManager(db, s)
    await _slot_or_422(manager, slot)
    row = await manager.save_login_credentials(slot, username, body.password, by=body.saved_by)
    await db.commit()
    return {
        "source": source,
        "slot": row.slot_number,
        "username": "CONFIGURED",
        "password": "CONFIGURED",
        "updated_at": row.login_credentials_updated_at.isoformat() if row.login_credentials_updated_at else None,
        "updated_by": row.login_credentials_updated_by,
    }


@router.post("/{source}/credentials/{slot}/clear", dependencies=[Depends(require_admin)])
async def clear_credentials(source: str, slot: int, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    manager = SessionManager(db, s)
    await _slot_or_422(manager, slot)
    row = await manager.clear_login_credentials(slot)
    await db.commit()
    return {"source": source, "slot": row.slot_number, "username": "NOT CONFIGURED", "password": "NOT CONFIGURED"}


@router.post("/{source}/login/start", dependencies=[Depends(require_admin)])
async def start_login(source: str, body: StartLogin, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    manager = SessionManager(db, s)
    row = await _slot_or_422(manager, body.slot)
    if not settings.ENCRYPTION_KEY:
        raise HTTPException(409, "ENCRYPTION_KEY is NOT CONFIGURED; storage state cannot be encrypted")
    if s.state == "HALTED":
        raise HTTPException(409, f"source is HALTED: {s.state_reason}; re-enable after admin review first")
    saved = manager.load_login_credentials(row) if body.use_saved_credentials else None
    auto_complete = bool(saved and body.auto_complete_if_empty and row.state == "EMPTY")
    viewport = {"width": body.viewport_width, "height": body.viewport_height} if body.viewport_width and body.viewport_height else None
    try:
        sess = await registry.start(
            source,
            body.slot,
            settings.PLS_LOGIN_URL if source == "PakistanLawSite" else s.source_url,
            started_by=body.started_by,
            viewport=viewport,
            saved_credentials=saved,
            auto_complete=auto_complete,
        )
    except LoginSessionError as exc:
        raise HTTPException(409, str(exc))
    note = "type credentials in the streamed browser"
    if saved:
        note = "saved credentials applied; confirm the page and complete login"
        if auto_complete:
            note = "saved credentials applied and sign-in was attempted; complete once authenticated"
    return {
        "status": sess.status,
        "slot": sess.slot_number,
        "viewport": sess.viewport,
        "stream": f"/admin/sessions/{source}/login/stream",
        "saved_credentials_used": bool(saved),
        "auto_complete_attempted": auto_complete,
        "autofill": sess.last_autofill,
        "note": note,
    }


@router.websocket("/{source}/login/stream")
async def login_stream(websocket: WebSocket, source: str):
    if not await require_admin_ws(websocket):
        return
    sess = registry.get(source)
    if sess is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    await sess.snapshot()  # a static page emits no screencast frame; show the operator something at once

    async def pump_frames():
        while sess.status not in ("closed",):
            frame = await sess.next_frame(timeout=2.0)
            if frame is None:
                await websocket.send_text(json.dumps({"type": "ping", "url": sess.last_url}))
                continue
            await websocket.send_text(json.dumps(frame))

    pump = asyncio.create_task(pump_frames())
    try:
        while True:
            msg = await websocket.receive_text()
            try:
                event = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if event.get("kind") in ("mouse", "key", "text", "press", "navigate"):
                try:
                    info = await sess.input_event(event)
                    if info is not None:
                        await websocket.send_text(json.dumps({"type": "focus", **info}))
                except LoginSessionError as exc:
                    await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
                except Exception as exc:
                    await websocket.send_text(json.dumps({"type": "error", "message": str(exc)[:200]}))
            elif event.get("kind") == "status":
                await websocket.send_text(json.dumps({"type": "status", **(await sess.is_authenticated())}))
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()


@router.post("/{source}/login/complete", dependencies=[Depends(require_admin)])
async def complete_login(source: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    manager = SessionManager(db, s)
    try:
        result = await registry.complete(source, manager)
    except LoginSessionError as exc:
        raise HTTPException(409, str(exc))
    await db.commit()
    return result


@router.post("/{source}/login/cancel", dependencies=[Depends(require_admin)])
async def cancel_login(source: str) -> Dict[str, Any]:
    return {"cancelled": await registry.cancel(source)}


@router.post("/{source}/slots/{slot}/{action}", dependencies=[Depends(require_admin)])
async def slot_action(source: str, slot: int, action: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    manager = SessionManager(db, s)
    row = await _slot_or_422(manager, slot)
    if action == "pause":
        row.state, row.state_reason = "PAUSED", "paused by admin"
    elif action == "resume":
        if not row.storage_state_encrypted:
            raise HTTPException(409, "slot has no storage state; human login required")
        if row.state == "HALTED":
            raise HTTPException(409, "HALTED slot: re-enable the source after admin review")
        row.state, row.state_reason = "ACTIVE", None
    elif action == "clear":
        row.storage_state_encrypted, row.storage_state_hash, row.state, row.state_reason = None, None, "EMPTY", "cleared by admin"
    else:
        raise HTTPException(422, "action must be pause|resume|clear")
    await db.commit()
    return {"slot": slot, "state": row.state}
