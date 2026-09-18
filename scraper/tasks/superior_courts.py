"""
Superior-court PUBLIC sources (Annex B-2): Supreme Court, Lahore, Sindh, Peshawar, Balochistan,
Islamabad, Azad Jammu & Kashmir High Court, Azad Jammu & Kashmir Supreme Court, Federal Shariat Court.

Each court is a set of listing URLs. Listings are fetched under robots + allow-list, judgment
documents (PDF or HTML) are written into crawl_frontier, and the shared PublicPipeline applies
the raw-first rule, original-PDF preservation, OCR fallback and hybrid extraction. Listing URLs
can be overridden per source from the dashboard via config_json["listings"].
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import ScraperSource
from scraper.tasks.public_pipeline import run_public_source

DEFAULT_LISTINGS: Dict[str, List[str]] = {
    "SupremeCourt": ["https://www.supremecourt.gov.pk/judgements/", "https://www.supremecourt.gov.pk/judgement-search/"],
    "LahoreHighCourt": ["https://sys.lhc.gov.pk/appjudgments/", "https://sys.lhc.gov.pk/appjudgments/Reported"],
    "SindhHighCourt": ["https://caselaw.shc.gov.pk/caselaw/view-file/", "https://www.shc.gov.pk/judgments"],
    "PeshawarHighCourt": ["https://www.peshawarhighcourt.gov.pk/app/site/judgments"],
    "BalochistanHighCourt": ["https://bhc.gov.pk/judgments"],
    "IslamabadHighCourt": ["https://mis.ihc.gov.pk/judgments", "https://www.ihc.gov.pk/judgments"],
    "AJKHighCourt": ["https://ajkhighcourt.gok.pk/important-judgments", "https://ajkhighcourt.gok.pk/important-judgments?judgment_tab=previous"],
    "AJKSupremeCourt": [
        "https://ajksupremecourt.gok.pk/judgements-orders/",
        "https://ajksupremecourt.gok.pk/category/judgments/",
        "https://scapp.ajksupremecourt.gok.pk/Judgements.php",
    ],
    "FederalShariatCourt": ["https://www.federalshariatcourt.gov.pk/en/judgments/"],
}

COURT_SOURCES = tuple(DEFAULT_LISTINGS.keys())


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("listings") or DEFAULT_LISTINGS.get(source.source_name, [source.source_url])
    return [{"url": u, "target_kind": "judgment"} for u in urls]


async def scrape_superior_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    if source.source_name == "FederalShariatCourt":
        from scraper.tasks.federal_shariat_court import scrape_federal_shariat_court

        return await scrape_federal_shariat_court(source, db, **kwargs)
    if source.source_name == "AJKHighCourt":
        from scraper.tasks.ajk_high_court import scrape_ajk_high_court

        return await scrape_ajk_high_court(source, db, **kwargs)
    if source.source_name == "AJKSupremeCourt":
        from scraper.tasks.ajk_supreme_court import scrape_ajk_supreme_court

        return await scrape_ajk_supreme_court(source, db, **kwargs)
    return await run_public_source(db, source, seed_listings=listings_for(source), **kwargs)
