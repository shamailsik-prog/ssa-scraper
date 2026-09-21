"""
Pull-off harvest layers for PakistanLawSite.

The base path (always on) is the working spec: permission gate, raw-first staging,
deterministic parsers, crawl_frontier seeding, crawl_coverage as the research ledger.

Everything else is a named layer. Disable the layer and the base path remains.

Layers
------
surface_adapter
    auto           use the live CitationSearch surface (grid if present, else form)
    form           spec §3.4 only — pull the citation-grid adapter off
    citation_grid  force the CaseNames table walk

skip_known_full
    Grid only. Skip a row whose canonical citation is already a full promoted judgment.
    Pull off to re-fetch every row.

reporter_shards
    On when login-session concurrency is 2. Slot/job 0 and 1 split subscribed reporters.
    Pull off by setting LOGIN_SESSION_CONCURRENCY=1 (or backfill equivalent).

harvest_pacing
    updates  = spec 4–9 s, 300/hour, 2500/day
    backfill = faster profile. Pull off by HARVEST_MODE=updates.

frontier_drain_after_grid
    After a grid window, also run Tiers 1–4 when the search map has form fields.
    Off by default so a grid-only site cannot hang on empty form submits.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from scraper.config import settings
from scraper.harvest_mode import login_pacing_profile
from scraper.parsers.citation_extractor import extract_citations

SURFACE_ADAPTERS = ("auto", "form", "citation_grid")


def surface_adapter() -> str:
    value = str(getattr(settings, "PLS_SURFACE_ADAPTER", "auto") or "auto").strip().lower()
    return value if value in SURFACE_ADAPTERS else "auto"


def skip_known_full_enabled() -> bool:
    return bool(getattr(settings, "PLS_SKIP_KNOWN_FULL_CITATIONS", True))


def frontier_drain_after_grid_enabled() -> bool:
    return bool(getattr(settings, "PLS_ALSO_DRAIN_FRONTIER", False))


def should_use_citation_grid(*, is_grid_map: bool) -> bool:
    adapter = surface_adapter()
    if adapter == "form":
        return False
    if adapter == "citation_grid":
        return True
    return bool(is_grid_map)


def volume_from_citation(citation: str) -> Optional[Dict[str, Any]]:
    """Parse reporter/year/page so a grid hit can credit spec Tier-1 coverage."""
    text = (citation or "").strip()
    if not text:
        return None
    hits = extract_citations(text)
    reporter = None
    year = None
    page = None
    if hits:
        reporter = hits[0].get("reporter")
        year = hits[0].get("year")
        page = hits[0].get("page")
    if not reporter:
        token = text.upper()
        for name in sorted(
            ("PLD", "SCMR", "CLC", "PCRLJ", "PTD", "PLC", "CLD", "YLR", "MLD", "GBLR", "PLJ", "NLR", "KLR", "PCRLJ"),
            key=len,
            reverse=True,
        ):
            if token == name or token.startswith(name + " ") or token.startswith(name + "-"):
                reporter = "PCrLJ" if name == "PCRLJ" else name
                break
        if reporter is None:
            reporter = token.split()[0] if token.split() else None
    if year is None:
        year_match = re.search(r"(19\d{2}|20\d{2})", text)
        year = int(year_match.group(1)) if year_match else None
    else:
        year = int(year)
    if page is None:
        page_match = re.search(r"(\d+[A-Za-z]?)\s*$", text)
        page = page_match.group(1) if page_match else None
    page_no = None
    if page is not None:
        digits = re.sub(r"\D", "", str(page))
        page_no = int(digits) if digits else None
    if not reporter or not year:
        return None
    return {"reporter": str(reporter), "year": int(year), "page": page_no}


def describe_layers(harvest_mode: str, reporter_shard: Optional[int] = None) -> Dict[str, Any]:
    """Operator-visible snapshot of which layers are on. Safe to show on /health."""
    pacing = login_pacing_profile(harvest_mode)  # type: ignore[arg-type]
    concurrency = int(pacing.get("login_session_concurrency") or 1)
    return {
        "base": "spec_frontier",
        "surface_adapter": surface_adapter(),
        "skip_known_full": skip_known_full_enabled(),
        "reporter_shards": concurrency >= 2,
        "reporter_shard": reporter_shard,
        "harvest_pacing": harvest_mode,
        "frontier_drain_after_grid": frontier_drain_after_grid_enabled(),
        "how_to_pull_off": {
            "surface_adapter": "PLS_SURFACE_ADAPTER=form",
            "skip_known_full": "PLS_SKIP_KNOWN_FULL_CITATIONS=false",
            "reporter_shards": "LOGIN_SESSION_CONCURRENCY=1",
            "harvest_pacing": "HARVEST_MODE=updates",
            "frontier_drain_after_grid": "PLS_ALSO_DRAIN_FRONTIER=false",
        },
    }
