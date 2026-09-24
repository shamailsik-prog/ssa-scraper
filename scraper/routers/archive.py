"""/admin/archive — archive targets, mirror runs and reconcile_storage (Section 9 / 9A).

"Connect Google Drive" (operator request, 24 September 2026): the operator pastes the OAuth
client id and secret of their own Google Cloud project once, presses Connect, signs in on
Google's page and presses Allow. The callback exchanges the code for a refresh token, creates the
folder "SIKANDER AI Corpus" in their Drive and stores everything Fernet-encrypted in a
google_drive target. No Google password ever reaches the service.
"""

from __future__ import annotations

import html
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import ARCHIVE_TARGET_TYPES, settings
from scraper.database import get_db
from scraper.models import ArchiveTarget
from scraper.routers.auth import require_admin
from scraper.storage.archive import ArchiveMirror, archive_status

router = APIRouter(prefix="/admin/archive", tags=["archive"], dependencies=[Depends(require_admin)])
# Google sends the browser back here after the operator presses Allow. The browser carries no admin
# key on that navigation, so the single-use `state` token issued by /admin/archive/google-drive/connect
# is the credential; it lives 15 minutes in Redis and is consumed on first use.
oauth_router = APIRouter(tags=["archive"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_EXCHANGE_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"  # only files and folders this service creates
# An existing folder (one an earlier service created) is invisible under drive.file, so writing into
# it needs the full Drive permission for that account.
GOOGLE_DRIVE_FULL_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_ROOT_FOLDER_NAME = "SIKANDER AI Corpus"
_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
OAUTH_CALLBACK_PATH = "/oauth/google-drive/callback"
_PENDING_KEY = "corpus:google_drive_oauth:{state}"
_PENDING_TTL_SECONDS = 900


class GoogleDriveConnect(BaseModel):
    client_id: str = Field(min_length=10, max_length=300)
    client_secret: str = Field(min_length=6, max_length=300)
    name: str = Field(default="google_drive", min_length=1, max_length=100)
    mirror_login_session_rows: bool = True
    # Optional: the link (or id) of a folder that already exists in that Drive, for example the one
    # an earlier service wrote into. Empty: a new folder "SIKANDER AI Corpus" is created.
    folder: Optional[str] = Field(default=None, max_length=400)


def folder_id_from(text: Optional[str]) -> Optional[str]:
    """The Drive folder id in a pasted link (`.../folders/<id>`, `?id=<id>`) or a bare id."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", text) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", text)
    candidate = m.group(1) if m else text
    if not _FOLDER_ID_RE.match(candidate):
        raise HTTPException(422, "folder must be a Google Drive folder link (https://drive.google.com/drive/folders/...) or a folder id")
    return candidate


async def _redis():
    import redis.asyncio as aioredis

    return aioredis.from_url(settings.REDIS_URL)


def _callback_url(request: Request) -> str:
    # Behind Caddy the API sees X-Forwarded-Proto/Host (uvicorn --proxy-headers); the operator's
    # browser must come back to the same public address that Google was told about.
    return f"{request.url.scheme}://{request.url.netloc}{OAUTH_CALLBACK_PATH}"


async def _exchange_code(*, code: str, client_id: str, client_secret: str, redirect_uri: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GOOGLE_OAUTH_EXCHANGE_ENDPOINT,
            data={"code": code, "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"Google refused the sign-in code (HTTP {resp.status_code}): {resp.text[:200]}")
    return resp.json()


async def _create_root_folder(access_token: str) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GOOGLE_DRIVE_FILES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"name": DRIVE_ROOT_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"Google Drive would not create the folder (HTTP {resp.status_code}): {resp.text[:200]}")
    return str(resp.json().get("id") or "")


async def _verify_folder(access_token: str, folder_id: str) -> str:
    """The name of an existing folder the granted account can reach (a Shared Drive included);
    anything else is refused before the target is stored."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{GOOGLE_DRIVE_FILES_URL}/{folder_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,name,mimeType,trashed", "supportsAllDrives": "true"},
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"Google Drive cannot open that folder with this account (HTTP {resp.status_code}): {resp.text[:200]}")
    meta = resp.json()
    if meta.get("mimeType") != "application/vnd.google-apps.folder" or meta.get("trashed"):
        raise HTTPException(502, "the link is not a folder that is still in the Drive")
    return str(meta.get("name") or folder_id)


@router.post("/google-drive/connect")
async def google_drive_connect(body: GoogleDriveConnect, request: Request) -> Dict[str, Any]:
    if not settings.ENCRYPTION_KEY:
        raise HTTPException(409, "ENCRYPTION_KEY is NOT CONFIGURED; the Drive permission cannot be stored")
    folder_id = folder_id_from(body.folder)
    state = secrets.token_urlsafe(32)
    redirect_uri = _callback_url(request)
    pending = {
        "client_id": body.client_id.strip(),
        "client_secret": body.client_secret.strip(),
        "name": body.name.strip(),
        "mirror_login_session_rows": body.mirror_login_session_rows,
        "folder_id": folder_id,
        "scope": GOOGLE_DRIVE_FULL_SCOPE if folder_id else GOOGLE_DRIVE_SCOPE,
        "redirect_uri": redirect_uri,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    r = await _redis()
    try:
        await r.set(_PENDING_KEY.format(state=state), settings.encrypt_value(json.dumps(pending)), ex=_PENDING_TTL_SECONDS)
    finally:
        await r.aclose()
    params = {
        "client_id": pending["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": pending["scope"],
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return {"authorization_url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}", "redirect_uri": redirect_uri, "expires_in_seconds": _PENDING_TTL_SECONDS}


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>body{{font:16px/1.5 Georgia,serif;margin:40px auto;max-width:640px;padding:0 16px;color:#1c1a17}}"
        f"a{{color:#7a1f1f}}</style></head><body><h1>{html.escape(title)}</h1>{body}"
        f"<p><a href='/dashboard'>Back to the dashboard</a> · <a href='/status'>Status page</a></p></body></html>",
        status_code=status_code,
    )


@oauth_router.get(OAUTH_CALLBACK_PATH, response_class=HTMLResponse)
async def google_drive_callback(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    state = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""
    error = request.query_params.get("error") or ""
    if not state:
        return _page("Google Drive not connected", "<p>The link is missing its state token. Start again from the dashboard's Archive tab.</p>", 400)
    r = await _redis()
    try:
        raw = await r.getdel(_PENDING_KEY.format(state=state))
    finally:
        await r.aclose()
    if not raw:
        return _page("Google Drive not connected", "<p>This link has expired or was already used (it is valid for 15 minutes, once). Press Connect again on the dashboard's Archive tab.</p>", 400)
    pending = json.loads(settings.decrypt_value(raw.decode() if isinstance(raw, bytes) else raw))
    if error or not code:
        return _page("Google Drive not connected", f"<p>Google did not grant access: {html.escape(error or 'no code returned')}.</p>", 400)
    try:
        tokens = await _exchange_code(code=code, client_id=pending["client_id"], client_secret=pending["client_secret"], redirect_uri=pending["redirect_uri"])
    except HTTPException as exc:
        return _page("Google Drive not connected", f"<p>{html.escape(str(exc.detail))}</p>", 502)
    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    if not refresh_token or not access_token:
        return _page(
            "Google Drive not connected",
            "<p>Google returned no lasting permission. On Google's screen remove this app under <em>Security → Third-party access</em>, then press Connect again so Google asks for consent afresh.</p>",
            502,
        )
    folder_id = pending.get("folder_id") or ""
    folder_name = DRIVE_ROOT_FOLDER_NAME
    try:
        if folder_id:
            folder_name = await _verify_folder(access_token, folder_id)
        else:
            folder_id = await _create_root_folder(access_token)
    except HTTPException as exc:
        return _page("Google Drive not connected", f"<p>{html.escape(str(exc.detail))}</p>", 502)
    if not folder_id:
        return _page("Google Drive not connected", "<p>Google Drive created no folder id.</p>", 502)
    name = pending["name"]
    existing = (await db.execute(select(ArchiveTarget).where(ArchiveTarget.name == name))).scalars().first()
    t = existing or ArchiveTarget(name=name, target_type="google_drive")
    t.target_type = "google_drive"
    t.root_path = ""
    t.enabled = True
    t.mirror_login_session_rows = bool(pending.get("mirror_login_session_rows"))
    t.consecutive_failures = 0
    t.last_error = None
    t.config_encrypted = settings.encrypt_value(
        json.dumps({"client_id": pending["client_id"], "client_secret": pending["client_secret"], "refresh_token": refresh_token, "folder_id": folder_id, "scope": pending.get("scope") or GOOGLE_DRIVE_SCOPE})
    )
    if existing is None:
        db.add(t)
    await db.commit()
    note = ""
    if t.mirror_login_session_rows and not settings.MIRROR_LOGIN_SESSION_ROWS:
        note = (
            "<p><strong>One more switch:</strong> PakistanLawSite judgments are copied only when the deployment flag "
            "<code>MIRROR_LOGIN_SESSION_ROWS</code> is also on. Run the deploy-cloud workflow once with "
            "<em>mirror_login_session_rows</em> set to <code>true</code>.</p>"
        )
    return _page(
        "Google Drive connected",
        f"<p>The service will copy judgments into the folder <strong>{html.escape(folder_name)}</strong> in your Google Drive on its next archive run (every 30 minutes).</p>{note}",
    )


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
    result["instruments"] = await mirror.mirror_instruments()
    await db.commit()
    return result


@router.post("/targets/{name}/test")
async def test_target(name: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    t = (await db.execute(select(ArchiveTarget).where(ArchiveTarget.name == name))).scalars().first()
    if t is None:
        raise HTTPException(404, "target not found")
    return {"name": t.name, **(await ArchiveMirror(db).test_target(t))}


@router.post("/reconcile")
async def reconcile_now(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    report = await ArchiveMirror(db).reconcile()
    await db.commit()
    return report
