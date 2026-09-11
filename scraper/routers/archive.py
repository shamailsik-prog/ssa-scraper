"""/admin/archive — archive targets, mirror runs and reconcile_storage (Section 9 / 9A)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import ARCHIVE_TARGET_TYPES, settings
from scraper.database import get_db
from scraper.models import ArchiveTarget
from scraper.routers.auth import require_admin
from scraper.storage.archive import ArchiveMirror, archive_status

router = APIRouter(prefix="/admin/archive", tags=["archive"], dependencies=[Depends(require_admin)])


class TargetIn(BaseModel):
    name: str
    target_type: str
    root_path: str = ""
    config: Optional[Dict[str, Any]] = None  # stored Fernet-encrypted; never returned
    enabled: bool = True
    mirror_login_session_rows: bool = False


@router.get("")
async def status(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    return {"mirror_login_session_rows_deployment_flag": settings.MIRROR_LOGIN_SESSION_ROWS, "target_types": list(ARCHIVE_TARGET_TYPES), "targets": await archive_status(db)}


@router.post("/targets")
async def create_target(body: TargetIn, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    if body.target_type not in ARCHIVE_TARGET_TYPES:
        raise HTTPException(422, f"target_type must be one of {ARCHIVE_TARGET_TYPES}")
    existing = (await db.execute(select(ArchiveTarget).where(ArchiveTarget.name == body.name))).scalars().first()
    t = existing or ArchiveTarget(name=body.name, target_type=body.target_type)
    t.target_type = body.target_type
    t.root_path = body.root_path
    t.enabled = body.enabled
    t.mirror_login_session_rows = body.mirror_login_session_rows
    if body.config is not None:
        if not settings.ENCRYPTION_KEY:
            raise HTTPException(409, "ENCRYPTION_KEY is NOT CONFIGURED; target configuration cannot be stored")
        t.config_encrypted = settings.encrypt_value(json.dumps(body.config))
    if existing is None:
        db.add(t)
    await db.commit()
    return {"name": t.name, "type": t.target_type, "enabled": t.enabled, "config": "CONFIGURED" if t.config_encrypted else "NOT CONFIGURED"}


@router.post("/targets/{name}/{action}")
async def target_action(name: str, action: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    t = (await db.execute(select(ArchiveTarget).where(ArchiveTarget.name == name))).scalars().first()
    if t is None:
        raise HTTPException(404, "target not found")
    if action == "enable":
        t.enabled = True
    elif action == "disable":
        t.enabled = False
    else:
        raise HTTPException(422, "action must be enable|disable")
    await db.commit()
    return {"name": t.name, "enabled": t.enabled}


@router.post("/mirror")
async def mirror_now(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    mirror = ArchiveMirror(db)
    result = await mirror.mirror_pending()
    result["statutes"] = await mirror.mirror_statutes()
    await db.commit()
    return result


@router.post("/reconcile")
async def reconcile_now(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    report = await ArchiveMirror(db).reconcile()
    await db.commit()
    return report
