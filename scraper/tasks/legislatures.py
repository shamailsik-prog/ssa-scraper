"""
Legislatures and the Gazette — PUBLIC statute/instrument sources (Annex B-2): National Assembly,
Senate, the four provincial assemblies and the Gazette of Pakistan.

Listing pages yield acts, ordinances, bills and notifications (PDF or HTML). Acts are ingested
as statutes (versioned sections); amendment acts, ordinances, notifications and gazette notices
as instruments that later bind to the statute sections they amend.
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import ScraperSource
from scraper.tasks.public_pipeline import run_public_source

DEFAULT_LISTINGS: Dict[str, List[Dict[str, Any]]] = {
    "NationalAssembly": [{"url": "https://na.gov.pk/en/legis.php", "target_kind": "instrument"}, {"url": "https://na.gov.pk/en/acts-tenure.php", "target_kind": "statute"}],
    "Senate": [{"url": "https://senate.gov.pk/en/legislation.php", "target_kind": "instrument"}],
    "PunjabAssembly": [{"url": "https://www.pap.gov.pk/acts", "target_kind": "statute"}, {"url": "https://punjablaws.gov.pk/index.html", "target_kind": "statute"}],
    "SindhAssembly": [{"url": "https://www.pas.gov.pk/index.php/acts", "target_kind": "statute"}, {"url": "https://sindhlaws.gov.pk/", "target_kind": "statute"}],
    "KPAssembly": [{"url": "https://www.pakp.gov.pk/acts/", "target_kind": "statute"}, {"url": "https://kpcode.kp.gov.pk/", "target_kind": "statute"}],
    "BalochistanAssembly": [{"url": "https://www.pabalochistan.gov.pk/acts", "target_kind": "statute"}],
    "GazetteOfPakistan": [{"url": "https://www.pcp.gov.pk/gazette", "target_kind": "instrument"}],
}

LEGISLATURE_SOURCES = tuple(DEFAULT_LISTINGS.keys())


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    if cfg.get("listings"):
        return [{"url": u, "target_kind": cfg.get("target_kind", "statute")} for u in cfg["listings"]]
    return DEFAULT_LISTINGS.get(source.source_name, [{"url": source.source_url, "target_kind": "statute"}])


async def scrape_legislature(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(db, source, seed_listings=listings_for(source), **kwargs)
