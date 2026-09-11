"""
NasirLawSite — PUBLIC case-law and statute source. Extraction mode stays hybrid until parser
quality is proven (Amendment §7). Reporter index pages are the listings; case pages are judgments.
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import ScraperSource
from scraper.tasks.public_pipeline import run_public_source

DEFAULT_LISTINGS = [
    {"url": "https://www.nasirlawsite.com/case/scmr.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/pld.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/clc.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/ylr.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/mld.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/laws/", "target_kind": "statute"},
]


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    if cfg.get("listings"):
        return [{"url": u, "target_kind": "judgment"} for u in cfg["listings"]]
    return DEFAULT_LISTINGS


async def scrape_nasirlawsite(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(db, source, seed_listings=listings_for(source), **kwargs)
