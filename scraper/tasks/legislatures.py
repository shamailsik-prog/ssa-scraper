"""
Legislatures and the Gazette — PUBLIC statute/instrument sources (Annex B-2): National Assembly,
Senate, the four provincial assemblies and the Gazette of Pakistan.

Listing pages yield acts, ordinances, bills and notifications (PDF or HTML). Acts are ingested
as statutes (versioned sections); amendment acts, ordinances, notifications and gazette notices
as instruments that later bind to the statute sections they amend.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.fetchers import has_pdf_signature
from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import ExplicitBlock, RobotsUnavailable, URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, _url_looks_like_pdf, run_public_source

logger = logging.getLogger(__name__)

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

PAB_HOST = "pabalochistan.gov.pk"
PAB_HOST_ALIASES = (PAB_HOST, "www.pabalochistan.gov.pk")
STORAGE_DOC_RE = re.compile(r"(?i)^/storage/\d+/.+\.(pdf|doc|docx)$")
LISTING_PATH_RE = re.compile(r"(?i)^/acts/?$")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    if cfg.get("listings"):
        return [{"url": u, "target_kind": cfg.get("target_kind", "statute")} for u in cfg["listings"]]
    return DEFAULT_LISTINGS.get(source.source_name, [{"url": source.source_url, "target_kind": "statute"}])


def normalize_pab_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public PAB hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PAB_HOST_ALIASES:
        scheme = "https"
        netloc = PAB_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def _classify_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if STORAGE_DOC_RE.search(path):
        return "document"
    if LISTING_PATH_RE.search(path):
        return "listing"
    return None


class BalochistanAssemblyPipeline(PublicPipeline):
    """Source-specific extraction for PAB acts tables and direct document links."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "statute")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: List[str] = []

        self._collect_structured_act_rows(
            html_text=res.text,
            base_url=res.final_url,
            docs=docs,
        )
        self._collect_listing_links(
            html_text=res.text,
            base_url=res.final_url,
            listings=listings,
        )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl in list(dict.fromkeys(listings)):
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={"kind": "listing", "url": nurl, "target_kind": target_kind, "depth": depth + 1},
                        cursor_json={},
                        priority=40,
                    )
                )
        await self.db.flush()

    def _collect_structured_act_rows(self, *, html_text: str, base_url: str, docs: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0

        for tenure_item in soup.select("#tenureAccordion > .accordion-item"):
            tenure_button = tenure_item.select_one(".accordion-header button")
            tenure = (tenure_button.get_text(" ", strip=True) if tenure_button else "")[:40]
            for year_item in tenure_item.select(".accordion-body .accordion-item"):
                year_button = year_item.select_one(".accordion-header button")
                year = (year_button.get_text(" ", strip=True) if year_button else "")[:8]
                if year and not YEAR_RE.search(year):
                    year = ""

                for table in year_item.select("table.table"):
                    headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
                    if "act no" not in headers or "act title" not in headers:
                        continue
                    for tr in table.select("tbody tr"):
                        cells = tr.find_all("td")
                        if len(cells) < 2:
                            continue
                        link = cells[1].find("a", href=True)
                        if link is None:
                            continue
                        row_index += 1
                        href = link.get("href", "")
                        title = link.get_text(" ", strip=True) or cells[1].get_text(" ", strip=True)
                        ext = (urlsplit(href).path.rsplit(".", 1)[-1].lower() if "." in (urlsplit(href).path or "") else "")

                        row_meta: Dict[str, Any] = {
                            "listing_fetch": "acts_table",
                            "discovery_channel": "acts-table-row",
                            "result_index": row_index,
                            "source_section": "acts",
                        }
                        if tenure:
                            row_meta["tenure"] = tenure
                        if year:
                            row_meta["act_year"] = year
                        act_no = cells[0].get_text(" ", strip=True)[:40]
                        if act_no:
                            row_meta["act_no"] = act_no
                        if title:
                            row_meta["act_title"] = title[:280]
                        if len(cells) > 2:
                            passed = cells[2].get_text(" ", strip=True)[:40]
                            if passed:
                                row_meta["act_passed_on"] = passed
                        if len(cells) > 3:
                            assented = cells[3].get_text(" ", strip=True)[:40]
                            if assented:
                                row_meta["act_assented_on"] = assented
                        if len(cells) > 4:
                            act_type = cells[4].get_text(" ", strip=True)[:80]
                            if act_type:
                                row_meta["act_type"] = act_type
                        if ext:
                            row_meta["document_format"] = ext
                            if ext == "pdf":
                                row_meta["expect_pdf"] = True

                        self._capture_candidate(
                            raw=href,
                            hint=title[:240],
                            base_url=base_url,
                            docs=docs,
                            listings=[],
                            route_meta=row_meta,
                        )

    def _collect_listing_links(self, *, html_text: str, base_url: str, listings: List[str]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs={},
                listings=listings,
                route_meta={},
            )

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_pab_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        kind = _classify_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "storage-file" if path.startswith("/storage/") else "direct-file",
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            listings.append(safe)

    async def _enqueue_documents_with_meta(
        self,
        docs: Dict[str, Dict[str, Any]],
        *,
        listing_url: str,
        default_target_kind: str,
    ) -> int:
        added = 0
        for url, meta in docs.items():
            kind = default_target_kind
            key = f"{kind}:{url}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is not None:
                continue

            route: Dict[str, Any] = {"listing": listing_url}
            for key_name in (
                "listing_fetch",
                "result_index",
                "source_section",
                "tenure",
                "act_year",
                "act_no",
                "act_title",
                "act_passed_on",
                "act_assented_on",
                "act_type",
                "document_format",
                "pdf_endpoint_kind",
            ):
                if key_name in meta:
                    route[key_name] = meta[key_name]
            self.db.add(
                CrawlFrontier(
                    source_name=self.source.source_name,
                    tier=0,
                    query_key=key,
                    query_json={
                        "kind": kind,
                        "url": url,
                        "route": route,
                        "meta": meta,
                        "expect_pdf": bool(meta.get("expect_pdf")),
                    },
                    cursor_json={},
                    priority=50,
                )
            )
            added += 1
        await self.db.flush()
        return added

    async def _drain_one(self, fr: CrawlFrontier) -> str:
        """
        Process one frontier row with PDF-signature gating for statute/instrument PDFs.

        Balochistan Assembly serves direct `/storage/...pdf` links; these must fail closed when
        the payload does not contain the PDF magic signature.
        """
        fr.status = "in_progress"
        fr.attempts += 1
        kind = fr.query_json.get("kind", "statute")
        url = fr.query_json.get("url")
        route = {**(fr.query_json.get("route") or {}), "frontier": fr.query_key}
        expect_pdf = bool(fr.query_json.get("expect_pdf"))
        try:
            res = await self.fetch(url)
            if res.verdict.kind in ("verification", "login"):
                fr.status = "retired"
                fr.last_error = f"page requires {res.verdict.kind}; public source has no login path"
            elif expect_pdf and not has_pdf_signature(res.content):
                fr.status = "retired"
                fr.last_error = f"missing %PDF signature for {kind} document URL"
            elif kind == "judgment" and (_url_looks_like_pdf(url) or res.is_pdf) and not has_pdf_signature(res.content):
                fr.status = "retired"
                fr.last_error = "missing %PDF signature for judgment document URL"
            elif res.status_code >= 400:
                fr.status = "retired" if res.status_code in (404, 410) else "pending"
                fr.last_error = f"HTTP {res.status_code}"
            else:
                if kind == "judgment":
                    await self.ingest_judgment(res, route=route, row_meta=fr.query_json.get("meta") or {})
                elif kind in ("statute", "instrument"):
                    await self.ingest_statute(res, route=route, kind=kind, meta=fr.query_json.get("meta") or {})
                elif kind == "listing":
                    await self.handle_listing(res, fr)
                fr.status = "done"
                fr.last_error = None
                fr.last_run_at = datetime.now(timezone.utc)
        except URLPolicyError as exc:
            fr.status = "retired"
            fr.last_error = str(exc)[:1000]
        except RobotsUnavailable as exc:
            fr.status = "pending"
            fr.attempts -= 1
            fr.last_error = str(exc)[:1000]
            self.stats["deferred"] += 1
        except ExplicitBlock:
            fr.status = "pending"
            await self.db.flush()
            return "halted"
        except Exception as exc:
            self.stats["errors"] += 1
            fr.last_error = str(exc)[:1000]
            fr.status = "pending" if fr.attempts < 3 else "retired"
            logger.exception("%s: frontier item failed %s", self.source.source_name, url)
        await self.db.flush()
        return "ok"


async def scrape_legislature(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    pipeline_cls = BalochistanAssemblyPipeline if source.source_name == "BalochistanAssembly" else PublicPipeline
    return await run_public_source(db, source, seed_listings=listings_for(source), pipeline_cls=pipeline_cls, **kwargs)
