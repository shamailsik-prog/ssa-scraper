"""
/status — a key-free, read-only page of numbers for the operator (requested 24 September 2026).

It shows how the corpus stands and when it last moved: totals, what was promoted today and this
week, each source's state and last success, the PakistanLawSite slots and today's pacing, the
archive targets (Google Drive included) with what they hold, and open notifications. Nothing more:
no judgment text, no URLs, no configuration values, no secrets. Every action and every record
still needs the admin key on /dashboard.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import get_db
from scraper.models import (
    ArchiveObject,
    ArchiveTarget,
    BrowserSessionSlot,
    Citation,
    Instrument,
    Judgment,
    Notification,
    QuarantineQueue,
    ScraperJob,
    ScraperSource,
    ScraperStaging,
    Statute,
    StatuteSection,
)

router = APIRouter(tags=["status"])

_TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"
_MANIFEST = (
    '{"name":"SIKANDER AI Corpus","short_name":"Corpus","start_url":"/status","display":"standalone",'
    '"background_color":"#f6f4ef","theme_color":"#7a1f1f",'
    '"icons":[{"src":"/static/icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any"}]}'
)
_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
    '<rect width="128" height="128" rx="24" fill="#7a1f1f"/>'
    '<path d="M28 40h72v10H28zM28 60h72v10H28zM28 80h48v10H28z" fill="#f6f4ef"/>'
    '<circle cx="96" cy="85" r="9" fill="#f6f4ef"/></svg>'
)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


async def status_payload(db: AsyncSession) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    judgments = await _count(db, select(func.count()).select_from(Judgment))
    latest_promotion = (await db.execute(select(func.max(Judgment.promoted_at)))).scalar()
    by_source = {
        name: int(n)
        for name, n in (await db.execute(select(Judgment.source_name, func.count()).group_by(Judgment.source_name))).all()
    }
    by_reporter_year: List[Dict[str, Any]] = [
        {"reporter": rep, "year": yr, "judgments": int(n)}
        for rep, yr, n in (
            await db.execute(
                select(Judgment.reporter, Judgment.year, func.count())
                .where(Judgment.reporter.isnot(None))
                .group_by(Judgment.reporter, Judgment.year)
                .order_by(Judgment.reporter, Judgment.year.desc())
            )
        ).all()
    ]
    staging_by_status = {
        status: int(n)
        for status, n in (await db.execute(select(ScraperStaging.status, func.count()).group_by(ScraperStaging.status))).all()
    }
    sources: List[Dict[str, Any]] = []
    for s in (await db.execute(select(ScraperSource).order_by(ScraperSource.source_name))).scalars().all():
        last_job = (
            await db.execute(
                select(ScraperJob).where(ScraperJob.source_name == s.source_name).order_by(ScraperJob.created_at.desc()).limit(1)
            )
        ).scalars().first()
        entry: Dict[str, Any] = {
            "source": s.source_name,
            "access": s.access_method,
            "state": s.state,
            "reason": s.state_reason,
            "last_success_at": _iso(s.last_success_at),
            "next_scrape_at": _iso(s.next_scrape_at),
            "judgments": by_source.get(s.source_name, 0),
            "last_job": None,
        }
        if last_job is not None:
            summary = last_job.result_summary if isinstance(last_job.result_summary, dict) else {}
            entry["last_job"] = {
                "status": last_job.status,
                "started_at": _iso(last_job.started_at),
                "finished_at": _iso(last_job.finished_at),
                "pages": int(last_job.pages_scraped or 0),
                "staged": int(last_job.records_extracted or 0),
                "outcome": summary.get("stop_reason") or summary.get("skipped") or (last_job.error_message or "")[:200] or None,
            }
        if s.access_method == "login_session":
            cfg = dict(s.config_json or {})
            entry["current_slot"] = cfg.get("current_slot")
            pacing = dict(cfg.get("pacing") or {})
            entry["pacing_today"] = {
                "day": pacing.get("day"),
                "pages_today": int(pacing.get("day_pages", 0) or 0),
                "pages_this_hour": int(pacing.get("hour_pages", 0) or 0) if pacing.get("hour") == now.strftime("%Y-%m-%dT%H") else 0,
                "limit_per_hour": settings.PAGES_PER_HOUR,
                "limit_per_day": settings.PAGES_PER_DAY,
            }
            entry["slots"] = [
                {"slot": sl.slot_number, "state": sl.state, "reason": sl.state_reason, "logged_in_at": _iso(sl.logged_in_at), "last_used_at": _iso(sl.last_used_at)}
                for sl in (
                    await db.execute(
                        select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == s.source_name).order_by(BrowserSessionSlot.slot_number)
                    )
                ).scalars().all()
            ]
        sources.append(entry)
    targets: List[Dict[str, Any]] = []
    for t in (await db.execute(select(ArchiveTarget).order_by(ArchiveTarget.name))).scalars().all():
        by_status = {
            status: int(n)
            for status, n in (
                await db.execute(select(ArchiveObject.status, func.count()).where(ArchiveObject.target_id == t.id).group_by(ArchiveObject.status))
            ).all()
        }
        judgments_mirrored = await _count(
            db,
            select(func.count(func.distinct(ArchiveObject.judgment_id))).where(
                ArchiveObject.target_id == t.id, ArchiveObject.status == "written", ArchiveObject.judgment_id.isnot(None)
            ),
        )
        last_written = (
            await db.execute(select(func.max(ArchiveObject.written_at)).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "written"))
        ).scalar()
        targets.append(
            {
                "name": t.name,
                "type": t.target_type,
                "enabled": t.enabled,
                "mirrors_pakistanlawsite": bool(settings.MIRROR_LOGIN_SESSION_ROWS and t.mirror_login_session_rows),
                "judgments_mirrored": judgments_mirrored,
                "objects": by_status,
                "objects_written_total": int(t.objects_written or 0),
                "bytes_written": int(t.bytes_written or 0),
                "last_written_at": _iso(last_written),
                "last_ok_at": _iso(t.last_ok_at),
                "last_reconciled_at": _iso(t.last_reconciled_at),
                "consecutive_failures": int(t.consecutive_failures or 0),
                "last_error": (t.last_error or "")[:300] or None,
            }
        )
    google_drive = [t for t in targets if t["type"] == "google_drive"]
    open_notifications = [
        {"level": n.level, "code": n.code, "source": n.source_name, "message": n.message[:300], "at": _iso(n.created_at)}
        for n in (
            await db.execute(select(Notification).where(Notification.acknowledged.is_(False)).order_by(Notification.created_at.desc()).limit(15))
        ).scalars().all()
    ]
    return {
        "generated_at": _iso(now),
        "totals": {
            "judgments": judgments,
            "citations": await _count(db, select(func.count()).select_from(Citation)),
            "statutes": await _count(db, select(func.count()).select_from(Statute)),
            "statute_sections": await _count(db, select(func.count()).select_from(StatuteSection)),
            "instruments": await _count(db, select(func.count()).select_from(Instrument)),
            "review_queue_open": await _count(db, select(func.count()).select_from(QuarantineQueue).where(QuarantineQueue.reviewed.is_(False))),
        },
        "movement": {
            "judgments_promoted_today": await _count(db, select(func.count()).select_from(Judgment).where(Judgment.promoted_at >= day_start)),
            "judgments_promoted_last_7_days": await _count(db, select(func.count()).select_from(Judgment).where(Judgment.promoted_at >= week_ago)),
            "last_promotion_at": _iso(latest_promotion),
            "staged_waiting_for_promotion": staging_by_status.get("extracted", 0),
            "staging_by_status": staging_by_status,
        },
        "judgments_by_source": by_source,
        "judgments_by_reporter_year": by_reporter_year,
        "sources": sources,
        "archive": {
            "google_drive": {
                "configured": bool(google_drive),
                "note": None
                if google_drive
                else "No Google Drive target exists. On the dashboard's Archive storage tab press Connect Google Drive and sign in with your Google account (docs/CLOUD_DEPLOYMENT.md, section 3a).",
                "targets": google_drive,
            },
            "targets": targets,
            "mirror_login_session_rows_setting": settings.MIRROR_LOGIN_SESSION_ROWS,
        },
        "open_notifications": open_notifications,
    }


@router.get("/status.json")
async def status_json(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    return await status_payload(db)


@router.get("/status", response_class=HTMLResponse)
async def status_page() -> str:
    return (_TEMPLATES / "status.html").read_text(encoding="utf-8")


@router.get("/manifest.webmanifest")
async def manifest() -> Response:
    return Response(content=_MANIFEST, media_type="application/manifest+json")


@router.get("/static/icon.svg")
async def icon() -> Response:
    return Response(content=_ICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
