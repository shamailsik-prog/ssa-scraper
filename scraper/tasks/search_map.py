"""
STEP 0 — map the PakistanLawSite search form (Amendment §10).

Playwright obtains the rendered DOM. Deterministic introspection runs first. The LOCAL
ScrapeGraph engine may assist in converting the DOM into a structured map, but every selector
it proposes is verified against the actual DOM before anything is saved. The map records
fields, result layout, page size, pagination, detail layout, limits, map_version, dom_hash and
mapped_at. Five consecutive result pages that fail parsing mark the map stale → alert → remap.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.extractors.deterministic import introspect_search_form
from scraper.extractors.scrapegraph_base import ExtractionInput
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.models import ScraperSource, SearchFormMap
from scraper.notify import notify

logger = logging.getLogger(__name__)
SAFE_FIELD_KINDS = {"select", "textarea", "text", "search", "number", "date", "email", "tel", "url", "password", "checkbox", "radio", "submit"}


def dom_hash(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    skeleton = " ".join(f"{t.name}:{t.get('name') or t.get('id') or ''}" for t in soup.find_all(["form", "input", "select", "table", "th", "a"])[:500])
    return hashlib.sha256(skeleton.encode()).hexdigest()


def verify_selectors(html: str, proposal: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only DOM-resolving controls that the browser can safely submit."""
    soup = BeautifulSoup(html or "", "html.parser")
    if proposal.get("surface") == "grid_surface_no_query_form":
        # A rendered CitationSearch grid may expose editable-looking DataTables filters.
        # They are not a query form and must never become fill targets.
        out = dict(proposal)
        out["fields"] = []
        return out
    verified_fields = []
    for f in proposal.get("fields") or []:
        sel = f.get("selector")
        try:
            hit = soup.select_one(sel) if sel else None
        except Exception:
            hit = None
        kind = f.get("kind")
        safe = (
            hit is not None
            and kind in SAFE_FIELD_KINDS
            and not hit.has_attr("disabled")
            and not hit.has_attr("readonly")
        )
        if safe:
            verified_fields.append(f)
    out = dict(proposal)
    out["fields"] = verified_fields
    for key in ("result_row_selector", "pagination_next_selector", "detail_link_selector"):
        sel = out.get(key)
        if sel:
            try:
                if soup.select_one(sel) is None:
                    out[key] = None
            except Exception:
                out[key] = None
    return out


def build_map_record(proposal: Dict[str, Any], html: str, mapped_by: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for f in proposal.get("fields") or []:
        role = f.get("role")
        if role and role not in fields:
            fields[role] = {"name": f["name"], "selector": f["selector"], "kind": f["kind"], "options": f.get("options") or []}
        fields.setdefault("_all", []).append({"name": f["name"], "selector": f["selector"], "kind": f["kind"], "role": role})
    reporter_opts = (fields.get("reporter") or {}).get("options") or []
    return {
        "fields": fields,
        "result_layout": {"row_selector": proposal.get("result_row_selector") or "table tr", "columns": proposal.get("result_columns") or {}, "detail_link_selector": proposal.get("detail_link_selector") or "a[href]"},
        "page_size": proposal.get("page_size"),
        "pagination": {"next_selector": proposal.get("pagination_next_selector")},
        "detail_layout": {"detail_link_selector": proposal.get("detail_link_selector") or "a[href]", "pdf_link_selector": "a[href$='.pdf']"},
        "limits": {
            "reporters_offered": reporter_opts[:100],
            "max_results_per_page": proposal.get("page_size"),
            "surface": proposal.get("surface") or "no_query_form",
        },
        "dom_hash": dom_hash(html),
        "mapped_by": mapped_by,
    }


async def active_map(db: AsyncSession, source_name: str) -> Optional[SearchFormMap]:
    return (
        await db.execute(select(SearchFormMap).where(SearchFormMap.source_name == source_name, SearchFormMap.is_active.is_(True)).order_by(SearchFormMap.map_version.desc()))
    ).scalars().first()


def map_as_dict(m: SearchFormMap) -> Dict[str, Any]:
    return {
        "fields": m.fields,
        "result_layout": m.result_layout,
        "page_size": m.page_size,
        "pagination": m.pagination,
        "detail_layout": m.detail_layout,
        "limits": m.limits,
        "surface": (m.limits or {}).get("surface"),
        "map_version": m.map_version,
        "dom_hash": m.dom_hash,
    }


async def map_search_form(db: AsyncSession, source: ScraperSource, html: str, *, local_engine: Optional[LocalScrapeGraphEngine] = None) -> SearchFormMap:
    """Create a new map version from the rendered search page HTML.

    A CitationSearch grid without its query form is recorded for diagnostics, but is
    immediately stale: its DataTables controls are not harvest inputs and the next
    runner invocation must render the page again.
    """
    proposal = introspect_search_form(html)
    mapped_by = "deterministic"
    engine = local_engine if local_engine is not None else LocalScrapeGraphEngine()
    if proposal.get("surface") != "grid_surface_no_query_form" and engine.configured and settings.SGAI_ENABLED and source.ai_extract_enabled:
        inp = ExtractionInput(source_name=source.source_name, access_method=source.access_method, content_hash=hashlib.sha256(html.encode()).hexdigest(), html=html, url=source.source_url)
        res = await engine.extract("search_form_map", inp)
        if res.ok and res.data:
            # merge: AI may add roles/selectors; every selector is re-verified against the DOM
            known = {f["name"] for f in proposal["fields"]}
            for f in res.data.get("fields") or []:
                if f.get("name") in known:
                    for pf in proposal["fields"]:
                        if pf["name"] == f["name"] and not pf.get("role") and f.get("role"):
                            pf["role"] = f["role"]
                else:
                    proposal["fields"].append(f)
            for key in ("result_row_selector", "pagination_next_selector", "detail_link_selector", "page_size"):
                if not proposal.get(key) and res.data.get(key):
                    proposal[key] = res.data[key]
            mapped_by = "deterministic+local_ai"
    verified = verify_selectors(html, proposal)
    record = build_map_record(verified, html, mapped_by)
    surface = record["limits"]["surface"]
    grid_surface = surface == "grid_surface_no_query_form"
    no_query_surface = surface == "no_query_form"
    field_count = len(record["fields"].get("_all") or [])
    mark_stale = grid_surface or (no_query_surface and field_count == 0)
    prev = await active_map(db, source.source_name)
    version = (prev.map_version + 1) if prev else 1
    if prev is not None:
        prev.is_active = False
    m = SearchFormMap(
        source_name=source.source_name,
        map_version=version,
        fields=record["fields"],
        result_layout=record["result_layout"],
        page_size=record["page_size"],
        pagination=record["pagination"],
        detail_layout=record["detail_layout"],
        limits=record["limits"],
        dom_hash=record["dom_hash"],
        mapped_by=mapped_by,
        verified_against_dom=True,
        is_active=True,
        stale=mark_stale,
        consecutive_parse_failures=settings.SEARCH_MAP_STALE_FAILURES if mark_stale else 0,
    )
    db.add(m)
    await db.flush()
    if grid_surface:
        await notify(
            db,
            level="error",
            code="SEARCH_MAP_GRID_SURFACE_STALE",
            message="CitationSearch grid-only surface captured; query form unavailable, map marked stale and remap required",
            source_name=source.source_name,
        )
        return m
    if no_query_surface and field_count == 0:
        await notify(
            db,
            level="error",
            code="SEARCH_MAP_NO_QUERY_FORM_STALE",
            message="CitationSearch mapped with no query form and no harvestable fields; map marked stale and remap required",
            source_name=source.source_name,
        )
        return m
    await notify(db, level="info", code="SEARCH_MAP_UPDATED", message=f"search form mapped (version {version}, {mapped_by}, {len(record['fields'].get('_all', []))} fields)", source_name=source.source_name)
    return m


async def record_parse_result(db: AsyncSession, m: SearchFormMap, *, ok: bool, source_name: str) -> bool:
    """Track consecutive result-page parse failures; returns True when the map has just gone stale."""
    if ok:
        m.consecutive_parse_failures = 0
        return False
    m.consecutive_parse_failures += 1
    if m.consecutive_parse_failures >= settings.SEARCH_MAP_STALE_FAILURES and not m.stale:
        m.stale = True
        await db.flush()
        await notify(db, level="error", code="search_map_stale", message=f"{m.consecutive_parse_failures} consecutive result pages failed parsing; remap required", source_name=source_name)
        return True
    await db.flush()
    return False


async def mark_map_stale(db: AsyncSession, m: SearchFormMap, *, source_name: str, reason: str) -> None:
    """Invalidate a map when a verified control is no longer usable at submit time."""
    if m.stale:
        return
    m.stale = True
    m.consecutive_parse_failures = max(m.consecutive_parse_failures, settings.SEARCH_MAP_STALE_FAILURES)
    await db.flush()
    await notify(
        db,
        level="error",
        code="search_map_stale",
        message=f"search form map is stale; remap required: {reason[:300]}",
        source_name=source_name,
    )
