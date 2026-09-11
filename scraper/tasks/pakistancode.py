"""
PakistanCode (Ministry of Law and Justice) — PUBLIC statute source.

Index pages list federal statutes; each statute page is preserved raw, split into sections by
the deterministic parser, optionally refined by ScrapeGraph (managed or local), validated, and
promoted as statute / statute_section / statute_section_version rows (version_confidence 0.5
where only current text exists — Annex B-5). Statute slugs can be pinned per deployment via
config_json["statute_urls"].
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import ScraperSource
from scraper.tasks.public_pipeline import run_public_source

DEFAULT_INDEXES = ["https://pakistancode.gov.pk/english/LGu3ZBxW1-apaUY2Fqa-apaUY2Fqa-sg-jjjjjjjjjjjjj", "https://pakistancode.gov.pk/federal"]


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("statute_urls") or cfg.get("listings") or DEFAULT_INDEXES
    return [{"url": u, "target_kind": "statute"} for u in urls]


async def scrape_pakistancode(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(db, source, seed_listings=listings_for(source), **kwargs)
