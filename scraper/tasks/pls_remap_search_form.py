"""Remap PakistanLawSite search_form_map from the authenticated dashboard Citation Search panel."""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import select

from scraper.auth.session_manager import ContinuityRunner, SessionManager, playwright_browser_factory
from scraper.config import settings
from scraper.database import SessionLocal
from scraper.extractors.deterministic import introspect_search_form
from scraper.models import ScraperSource
from scraper.pls_navigation import pls_check_url
from scraper.search_form_roles import verify_harvest_role_samples
from scraper.tasks.search_map import active_map, build_map_record, map_as_dict, map_search_form, verify_selectors

logger = logging.getLogger(__name__)
SOURCE_NAME = "PakistanLawSite"


class _ReadOnlyContinuityRunner(ContinuityRunner):
    """Dry-run remap: read an already-logged-in slot without leasing, touching, or config writes."""

    async def ensure_browser(self):
        if self.browser is not None:
            return self.browser
        for slot in await self.manager.slots():
            if slot.state == "ACTIVE":
                return await self.open(slot)
        raise RuntimeError("dry-run remap requires an ACTIVE login slot; no slot lease or login attempted")

    async def run(self, op, *, timeout=None):
        browser = await self.ensure_browser()
        limit = float(timeout if timeout is not None else settings.PLS_BROWSER_OP_TIMEOUT_SECONDS)
        return await self._call(op, browser, limit)


async def run_remap_search_form(*, dry_run: bool = False) -> Dict[str, Any]:
    async with SessionLocal() as db:
        source = (
            await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))
        ).scalars().first()
        if source is None:
            return {"ok": False, "error": "PakistanLawSite source missing"}
        before = await active_map(db, SOURCE_NAME)
        before_info = {
            "map_version": before.map_version if before else None,
            "stale": before.stale if before else None,
            "surface": (before.limits or {}).get("surface") if before else None,
            "field_roles": sorted(k for k in (before.fields or {}) if k != "_all") if before else [],
        }
        manager = SessionManager(db, source)
        runner_cls = _ReadOnlyContinuityRunner if dry_run else ContinuityRunner
        runner = runner_cls(manager, playwright_browser_factory)
        try:
            async def op(browser):
                page = await browser.goto(pls_check_url())
                return page

            page = await runner.run(op)
            probe = introspect_search_form(page.html or "")
            if probe.get("surface") != "query_form" or not (probe.get("fields") or []):
                out = {
                    "ok": False,
                    "error": "dashboard did not expose Citation Search query_form",
                    "surface": probe.get("surface"),
                    "n_fields": len(probe.get("fields") or []),
                    "before": before_info,
                    "url": page.url,
                }
                if dry_run:
                    await db.rollback()
                return out
            if dry_run:
                record = build_map_record(verify_selectors(page.html or "", probe), page.html or "", "dry_run")
                harvest_proof = verify_harvest_role_samples(record)
                out = {
                    "ok": True,
                    "dry_run": True,
                    "read_only": True,
                    "before": before_info,
                    "would_map_roles": [f.get("role") for f in probe["fields"]],
                    "reporters": next((f.get("options") for f in probe["fields"] if f.get("role") == "reporter"), []),
                    "harvest_role_proof": harvest_proof,
                    "url": page.url,
                }
                await db.rollback()
                return out
            m = await map_search_form(db, source, page.html or "", page_url=page.url or "")
            # Keep grid walk available: dual map (dashboard fields + archivedpatientGrid layout).
            from scraper.extractors.deterministic import enrich_dashboard_result_layout

            layout = dict(m.result_layout or {})
            layout["row_selector"] = layout.get("row_selector") or "#archivedpatientGrid tbody tr"
            if "archivedpatientgrid" not in str(layout.get("row_selector") or "").lower():
                layout["row_selector"] = "#archivedpatientGrid tbody tr"
            limits = dict(m.limits or {})
            limits["surface"] = "query_form"
            limits["dashboard_citation_fields"] = True
            m.result_layout = enrich_dashboard_result_layout(layout, limits)
            m.limits = limits
            m.stale = False
            await db.commit()
            after = map_as_dict(m)
            harvest_proof = verify_harvest_role_samples(after)
            return {
                "ok": True,
                "before": before_info,
                "harvest_role_proof": harvest_proof,
                "after": {
                    "map_version": m.map_version,
                    "stale": m.stale,
                    "surface": (m.limits or {}).get("surface"),
                    "field_roles": sorted(k for k in (after.get("fields") or {}) if k != "_all"),
                    "reporters_offered": (m.limits or {}).get("reporters_offered"),
                    "row_selector": layout.get("row_selector"),
                },
                "url": page.url,
            }
        finally:
            try:
                await runner.close()
            except Exception:
                pass
