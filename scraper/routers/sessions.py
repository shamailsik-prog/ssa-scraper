"""
/admin/sessions — login-session slots and the streamed human login (Amendment §9).

    POST  /admin/sessions/{source}/login/start   {slot}      → opens a server-side browser
    WS    /admin/sessions/{source}/login/stream?key=…        → JPEG frames out, input events in
    POST  /admin/sessions/{source}/login/complete             → verifies auth, stores encrypted state
    POST  /admin/sessions/{source}/login/cancel
    GET   /admin/sessions/{source}                            → slot states
    POST  /admin/sessions/{source}/slots/{n}/{pause|resume|clear}
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.auth.browser_login import LoginSessionError, registry
from scraper.auth.session_manager import SessionManager
from scraper.config import settings
from scraper.database import SessionLocal, get_db
from scraper.models import BrowserSessionSlot, ScraperSource
from scraper.routers.auth import require_admin, require_admin_ws

router = APIRouter(prefix="/admin/sessions", tags=["sessions"])


class StartLogin(BaseModel):
    slot: int = Field(default=1, ge=1, le=2)
    started_by: str = "operator"


async def _source(db: AsyncSession, name: str) -> ScraperSource:
    s = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == name))).scalars().first()
    if s is None or s.access_method != "login_session":
        raise HTTPException(404, "login_session source not found")
    return s


@router.get("/{source}", dependencies=[Depends(require_admin)])
async def slots(source: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    rows = (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == source).order_by(BrowserSessionSlot.slot_number))).scalars().all()
    return {
        "source": source,
        "state": s.state,
        "state_reason": s.state_reason,
        "login_scraping_permitted": settings.login_scraping_effective,
        "current_slot": (s.config_json or {}).get("current_slot"),
        "slots": [{"slot": r.slot_number, "role": r.role, "state": r.state, "reason": r.state_reason, "logged_in_at": r.logged_in_at.isoformat() if r.logged_in_at else None, "logged_in_by": r.logged_in_by, "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None, "reconnects": r.reconnect_count} for r in rows],
        "open_login_sessions": registry.status(),
    }


@router.post("/{source}/login/start", dependencies=[Depends(require_admin)])
async def start_login(source: str, body: StartLogin, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    s = await _source(db, source)
    if not settings.ENCRYPTION_KEY:
        raise HTTPException(409, "ENCRYPTION_KEY is NOT CONFIGURED; storage state cannot be encrypted")
    if s.state == "HALTED":
        raise HTTPException(409, f"source is HALTED: {s.state_reason}; re-enable after admin review first")
    try:
        sess = await registry.start(source, body.slot, settings.PLS_LOGIN_URL if source == "PakistanLawSite" else s.source_url, started_by=body.started_by)
    except LoginSessionError as exc:
        raise HTTPException(409, str(exc))
    return {"status": sess.status, "slot": sess.slot_number, "stream": f"/admin/sessions/{source}/login/stream", "note": "type credentials in the streamed browser; the service never sees them"}


@router.websocket("/{source}/login/stream")
async def login_stream(websocket: WebSocket, source: str):
    if not await require_admin_ws(websocket):
        return
    sess = registry.get(source)
    if sess is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()

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
            if event.get("kind") in ("mouse", "key", "navigate"):
                try:
                    await sess.input_event(event)
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
    row = await manager.slot(slot)
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
