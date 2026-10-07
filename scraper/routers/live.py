"""Read-only live dashboard: /live (page) and /live.json (coverage, rate, ETA, watchdog). Counts, times and
states only; no record text, URL, credential or configuration value. Admin actions stay behind the admin key."""

from __future__ import annotations

import pathlib
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import get_db
from scraper.models import BrowserSessionSlot, ScraperSource

router = APIRouter(tags=["live"])
_TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"


@router.get("/live.json")
async def live_json(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    from scraper.coverage import coverage_payload

    payload = await coverage_payload(db)
    payload["slots"] = [
        {"slot": sl.slot_number, "state": sl.state}
        for sl in (
            await db.execute(
                select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite").order_by(BrowserSessionSlot.slot_number)
            )
        ).scalars().all()
    ]
    payload["sources"] = {s.source_name: s.state for s in (await db.execute(select(ScraperSource).order_by(ScraperSource.source_name))).scalars().all()}
    return payload


@router.get("/live", response_class=HTMLResponse)
async def live_page() -> str:
    return (_TEMPLATES / "live.html").read_text(encoding="utf-8")
