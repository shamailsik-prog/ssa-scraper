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
    "KPAssembly": [{"url": "https://www.pakp.gov.pk/act/", "target_kind": "statute"}, {"url": "https://kpcode.kp.gov.pk/", "target_kind": "statute"}],
    "BalochistanAssembly": [{"url": "https://www.pabalochistan.gov.pk/acts", "target_kind": "statute"}],
    "GazetteOfPakistan": [{"url": "https://www.pcp.gov.pk/gazette", "target_kind": "instrument"}],
}

LEGISLATURE_SOURCES = tuple(DEFAULT_LISTINGS.keys())

PAB_HOST = "pabalochistan.gov.pk"
PAB_HOST_ALIASES = (PAB_HOST, "www.pabalochistan.gov.pk")
STORAGE_DOC_RE = re.compile(r"(?i)^/storage/\d+/.+\.(pdf|doc|docx)$")
LISTING_PATH_RE = re.compile(r"(?i)^/acts/?$")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
PAP_HOST = "pap.gov.pk"
PAP_HOST_ALIASES = (PAP_HOST, "www.pap.gov.pk")
PAP_ACT_DOC_RE = re.compile(r"(?i)^/uploads/acts/.+\.(pdf|html?)$")
PAP_LISTING_PATH_RE = re.compile(r"(?i)^/(acts/?|en/about-assembly/parliamentary-privileges/?)$")
PAS_HOST = "pas.gov.pk"
PAS_HOST_ALIASES = (PAS_HOST, "www.pas.gov.pk")
PAS_ACT_DOC_RE = re.compile(r"(?i)^/uploads/acts/.+\.(pdf|doc|docx|html?)$")
PAS_LISTING_PATH_RE = re.compile(r"(?i)^/index\.php/acts/?$")
PAS_DETAIL_PATH_RE = re.compile(r"(?i)^/index\.php/acts/details/\d+/\d+/?$")
PAKP_HOST = "pakp.gov.pk"
PAKP_HOST_ALIASES = (PAKP_HOST, "www.pakp.gov.pk")
PAKP_ACT_DOC_RE = re.compile(r"(?i)^/wp-content/uploads/.+\.(pdf|doc|docx|html?)$")
PAKP_LISTING_PATH_RE = re.compile(r"(?i)^/(act|acts)/?$")
PAKP_DETAIL_PATH_RE = re.compile(r"(?i)^/act/[^/?#]+/?$")


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


def normalize_pap_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public PAP hosts."""
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
    if host in PAP_HOST_ALIASES:
        scheme = "https"
        netloc = PAP_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_pas_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public PAS hosts."""
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
    if host in PAS_HOST_ALIASES:
        scheme = "https"
        netloc = PAS_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_pakp_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public PAKP hosts."""
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
    if host in PAKP_HOST_ALIASES:
        scheme = "https"
        netloc = PAKP_HOST + (f":{parts.port}" if parts.port else "")
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


def _classify_pap_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if PAP_ACT_DOC_RE.search(path):
        return "document"
    if PAP_LISTING_PATH_RE.search(path):
        return "listing"
    return None


def _classify_pas_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if PAS_ACT_DOC_RE.search(path):
        return "document"
    if PAS_LISTING_PATH_RE.search(path) or PAS_DETAIL_PATH_RE.search(path):
        return "listing"
    return None


def _classify_pakp_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if PAKP_ACT_DOC_RE.search(path):
        return "document"
    if PAKP_LISTING_PATH_RE.search(path) or PAKP_DETAIL_PATH_RE.search(path):
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
                "detail_fetch",
                "discovery_channel",
                "result_index",
                "source_section",
                "tenure",
                "act_year",
                "act_no",
                "act_title",
                "act_passed_on",
                "act_assented_on",
                "act_type",
                "detail_url",
                "detail_title",
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


class PunjabAssemblyPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for PAP acts tables and direct document links."""

    def _collect_structured_act_rows(self, *, html_text: str, base_url: str, docs: Dict[str, Dict[str, Any]]) -> None:  # type: ignore[override]
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0

        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_acts_table(headers):
                continue

            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                link = self._row_link(cells, headers)
                if link is None:
                    continue

                row_index += 1
                href = link.get("href", "")
                title = link.get_text(" ", strip=True) or cells[min(len(cells) - 1, 1)].get_text(" ", strip=True)
                ext = (urlsplit(href).path.rsplit(".", 1)[-1].lower() if "." in (urlsplit(href).path or "") else "")

                row_meta: Dict[str, Any] = {
                    "listing_fetch": "acts_table",
                    "discovery_channel": "acts-table-row",
                    "result_index": row_index,
                    "source_section": "acts",
                }
                self._add_row_provenance(row_meta=row_meta, cells=cells, headers=headers, title=title)

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

    @staticmethod
    def _looks_like_acts_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_no = any("act no" in h or "act number" in h or "act #" in h for h in headers)
        has_title = any("act title" in h or h == "title" for h in headers)
        return has_no and has_title

    @staticmethod
    def _row_link(cells: List[Any], headers: List[str]):
        title_idx = next((i for i, h in enumerate(headers) if "act title" in h or h == "title"), None)
        if title_idx is not None and title_idx < len(cells):
            link = cells[title_idx].find("a", href=True)
            if link is not None:
                return link
        for cell in cells:
            link = cell.find("a", href=True)
            if link is not None:
                return link
        return None

    def _add_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str], title: str) -> None:
        act_no_idx = next((i for i, h in enumerate(headers) if "act no" in h or "act number" in h or "act #" in h), None)
        if act_no_idx is not None and act_no_idx < len(cells):
            act_no = cells[act_no_idx].get_text(" ", strip=True)[:40]
            if act_no:
                row_meta["act_no"] = act_no

        if title:
            row_meta["act_title"] = title[:280]

        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue
            if "passed" in header:
                row_meta["act_passed_on"] = value[:40]
            elif "assent" in header:
                row_meta["act_assented_on"] = value[:40]
            elif "type" in header:
                row_meta["act_type"] = value[:80]
            elif "year" in header and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        if "act_year" not in row_meta:
            year_match = YEAR_RE.search(title or "")
            if year_match:
                row_meta["act_year"] = year_match.group(0)

    def _capture_candidate(  # type: ignore[override]
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_pap_public_url(raw, base_url=base_url)
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

        kind = _classify_pap_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "uploads-acts-file" if path.startswith("/uploads/acts/") else "direct-file",
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


class SindhAssemblyPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for PAS listing rows + detail-page act file routing."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "statute")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_detail_listing(res.final_url):
            self._collect_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
        else:
            self._collect_structured_listing_rows(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
            )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
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
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "result_index",
                    "source_section",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_assented_on",
                    "detail_url",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        return PAS_DETAIL_PATH_RE.search((urlsplit(url).path or "").lower()) is not None

    def _collect_structured_listing_rows(self, *, html_text: str, base_url: str, listings: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_listing_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                link = self._detail_link(cells, headers)
                if link is None:
                    continue
                row_index += 1
                title = link.get("title", "").strip() or link.get_text(" ", strip=True) or cells[1].get_text(" ", strip=True)
                row_meta: Dict[str, Any] = {
                    "listing_fetch": "acts_table",
                    "discovery_channel": "acts-table-row",
                    "source_section": "acts",
                    "result_index": row_index,
                }
                self._add_listing_row_provenance(row_meta=row_meta, cells=cells, headers=headers, title=title)
                self._capture_candidate(raw=link.get("href", ""), hint=title[:240], base_url=base_url, docs={}, listings=listings, route_meta=row_meta)

    @staticmethod
    def _looks_like_listing_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_no = any("act no" in h for h in headers)
        has_title = any("title" in h for h in headers)
        return has_no and has_title

    @staticmethod
    def _detail_link(cells: List[Any], headers: List[str]):
        title_idx = next((i for i, h in enumerate(headers) if "title" in h), None)
        if title_idx is not None and title_idx < len(cells):
            link = cells[title_idx].find("a", href=True)
            if link is not None:
                return link
        for cell in cells:
            link = cell.find("a", href=True)
            if link is not None:
                return link
        return None

    def _add_listing_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str], title: str) -> None:
        act_no_idx = next((i for i, h in enumerate(headers) if "act no" in h), None)
        if act_no_idx is not None and act_no_idx < len(cells):
            act_no = cells[act_no_idx].get_text(" ", strip=True)[:80]
            if act_no:
                row_meta["act_no"] = act_no

        if title:
            row_meta["act_title"] = title[:280]

        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue
            if "passing" in header:
                row_meta["act_passed_on"] = value[:40]
                if "act_year" not in row_meta and YEAR_RE.search(value):
                    row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
            elif "governor" in header or "assent" in header:
                row_meta["act_assented_on"] = value[:40]
                if "act_year" not in row_meta and YEAR_RE.search(value):
                    row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
            elif "year" in header and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        if "act_year" not in row_meta:
            year_match = YEAR_RE.search(title or "") or YEAR_RE.search(row_meta.get("act_no", ""))
            if year_match:
                row_meta["act_year"] = year_match.group(0)

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title = (soup.select_one("h2.act-title") or soup.select_one("h1") or soup.select_one("title"))
        detail_title_text = detail_title.get_text(" ", strip=True)[:280] if detail_title else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title_text and not base_meta.get("act_title"):
            base_meta["act_title"] = detail_title_text
        if detail_title_text:
            base_meta["detail_title"] = detail_title_text

        for p in soup.select("p"):
            label_node = p.find("label")
            if label_node is None:
                continue
            label = label_node.get_text(" ", strip=True).lower()
            value = p.get_text(" ", strip=True).replace(label_node.get_text(" ", strip=True), "", 1).strip(" :")
            if not value:
                continue
            if "act no" in label and "act_no" not in base_meta:
                base_meta["act_no"] = value[:80]
            elif ("passed on" in label or "date of passing" in label) and "act_passed_on" not in base_meta:
                base_meta["act_passed_on"] = value[:40]
            elif ("assent" in label or "enforcement" in label) and "act_assented_on" not in base_meta:
                base_meta["act_assented_on"] = value[:40]
            elif "subject" in label and "act_type" not in base_meta:
                base_meta["act_type"] = value[:80]
            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        for a in soup.find_all("a", href=True):
            hint = a.get_text(" ", strip=True)[:240]
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "act_files_section"
            route_meta["discovery_channel"] = "act-detail-file-link"
            self._capture_candidate(raw=a.get("href", ""), hint=hint, base_url=base_url, docs=docs, listings={}, route_meta=route_meta)

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_pas_public_url(raw, base_url=base_url)
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

        kind = _classify_pas_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "uploads-acts-file" if path.startswith("/uploads/acts/") else "direct-file",
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
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value


class KPAssemblyPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for PAKP acts table rows + detail-page document routing."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "statute")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_detail_listing(res.final_url):
            self._collect_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
        else:
            self._collect_structured_listing_rows(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
            )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
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
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "result_index",
                    "source_section",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_assented_on",
                    "detail_url",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        return PAKP_DETAIL_PATH_RE.search((urlsplit(url).path or "").lower()) is not None

    def _collect_structured_listing_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_listing_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 3:
                    continue
                links = tr.find_all("a", href=True)
                if not links:
                    continue
                row_index += 1
                title_link = self._title_link(cells, headers)
                title = (
                    title_link.get_text(" ", strip=True)
                    if title_link is not None
                    else cells[min(len(cells) - 1, 2)].get_text(" ", strip=True)
                )
                row_meta: Dict[str, Any] = {
                    "listing_fetch": "acts_table",
                    "discovery_channel": "acts-table-row",
                    "source_section": "acts",
                    "result_index": row_index,
                }
                self._add_listing_row_provenance(row_meta=row_meta, cells=cells, headers=headers, title=title)
                for link in links:
                    hint = link.get_text(" ", strip=True)[:240]
                    self._capture_candidate(
                        raw=link.get("href", ""),
                        hint=hint,
                        base_url=base_url,
                        docs=docs,
                        listings=listings,
                        route_meta=row_meta,
                    )

    @staticmethod
    def _looks_like_listing_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_act_no = any("act #" in h or "act no" in h or "act number" in h for h in headers)
        has_title = any("title" in h for h in headers)
        return has_act_no and has_title

    @staticmethod
    def _title_link(cells: List[Any], headers: List[str]):
        title_idx = next((i for i, h in enumerate(headers) if "title" in h), None)
        if title_idx is not None and title_idx < len(cells):
            link = cells[title_idx].find("a", href=True)
            if link is not None:
                return link
        for cell in cells:
            link = cell.find("a", href=True)
            if link is not None:
                return link
        return None

    def _add_listing_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str], title: str) -> None:
        act_no_idx = next((i for i, h in enumerate(headers) if "act #" in h or "act no" in h or "act number" in h), None)
        if act_no_idx is not None and act_no_idx < len(cells):
            act_no = cells[act_no_idx].get_text(" ", strip=True)[:80]
            if act_no:
                row_meta["act_no"] = act_no

        if title:
            row_meta["act_title"] = title[:280]

        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue
            if "passage" in header or "passed" in header:
                row_meta["act_passed_on"] = value[:40]
            elif "enforcement" in header or "assent" in header:
                row_meta["act_assented_on"] = value[:40]
            elif "year" in header and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

            if "act_year" not in row_meta and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        if "act_year" not in row_meta:
            year_match = YEAR_RE.search(title or "") or YEAR_RE.search(row_meta.get("act_no", ""))
            if year_match:
                row_meta["act_year"] = year_match.group(0)

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one(".sinpost-content h1") or soup.select_one("h1") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title and not base_meta.get("act_title"):
            base_meta["act_title"] = detail_title
        if detail_title:
            base_meta["detail_title"] = detail_title

        for row in soup.select(".leg-row"):
            label_node = row.select_one(".act-title")
            value_node = row.select_one(".act-info")
            if label_node is None or value_node is None:
                continue
            label = label_node.get_text(" ", strip=True).strip(": ").lower()
            value = value_node.get_text(" ", strip=True)
            if not value:
                continue
            if "act #" in label and "act_no" not in base_meta:
                base_meta["act_no"] = value[:80]
            elif "passage date" in label and "act_passed_on" not in base_meta:
                base_meta["act_passed_on"] = value[:40]
            elif ("enforcement" in label or "assent" in label) and "act_assented_on" not in base_meta:
                base_meta["act_assented_on"] = value[:40]
            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

            if "document" in label:
                for a in value_node.select("a[href]"):
                    route_meta = dict(base_meta)
                    route_meta["detail_fetch"] = "act_document_row"
                    route_meta["discovery_channel"] = "act-detail-file-link"
                    self._capture_candidate(
                        raw=a.get("href", ""),
                        hint=a.get_text(" ", strip=True)[:240],
                        base_url=base_url,
                        docs=docs,
                        listings={},
                        route_meta=route_meta,
                    )

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_pakp_public_url(raw, base_url=base_url)
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

        kind = _classify_pakp_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "wp-content-uploads-file" if path.startswith("/wp-content/uploads/") else "direct-file",
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
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value


async def scrape_legislature(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    pipeline_cls = PublicPipeline
    if source.source_name == "BalochistanAssembly":
        pipeline_cls = BalochistanAssemblyPipeline
    elif source.source_name == "PunjabAssembly":
        pipeline_cls = PunjabAssemblyPipeline
    elif source.source_name == "SindhAssembly":
        pipeline_cls = SindhAssemblyPipeline
    elif source.source_name == "KPAssembly":
        pipeline_cls = KPAssemblyPipeline
    return await run_public_source(db, source, seed_listings=listings_for(source), pipeline_cls=pipeline_cls, **kwargs)
