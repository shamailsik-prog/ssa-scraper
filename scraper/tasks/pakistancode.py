"""
PakistanCode (Ministry of Law and Justice) — PUBLIC statute/instrument source.

Live PakistanCode pages expose law discovery through alphabetical / chronological / category
indexes that link to encoded detail pages, with the official document served as downloadable PDF
(`pdffiles/...pdf`) and via ViewerJS embeds.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.fetchers import has_pdf_signature
from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import ExplicitBlock, RobotsUnavailable, URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

logger = logging.getLogger(__name__)

PAKISTANCODE_HOST = "pakistancode.gov.pk"
PAKISTANCODE_HOST_ALIASES = (PAKISTANCODE_HOST, "www.pakistancode.gov.pk")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
ACT_NO_RE = re.compile(r"\b([IVXLCDM]{1,15}\s+of\s+\d{4})\b", re.IGNORECASE)
PROMULGATION_RE = re.compile(r"Promulgation\s+Date:\s*([A-Za-z]+\s+\d{1,2}\s+\d{4})", re.IGNORECASE)
DETAIL_PATH_RE = re.compile(r"(?i)^/english/uy2fqajw1-[^/?#]+$")
LISTING_PATH_RE = re.compile(
    r"(?i)^/english/(?:index\.php|lgu0xad(?:\.php)?|lgu0xbd(?:\.php)?|lgu0xvd(?:\.php)?|lgu0xvd-[^/?#]+|ldj0xvd(?:\.php)?)$"
)
PDF_PATH_RE = re.compile(r"(?i)^/pdffiles/.+\.pdf$")
VIEWER_PATH_RE = re.compile(r"(?i)^/viewerjs/?$")

DEFAULT_LISTINGS: List[Dict[str, str]] = [
    {"url": "https://pakistancode.gov.pk/english/index.php", "target_kind": "statute"},
    {"url": "https://pakistancode.gov.pk/english/LGu0xAD.php", "target_kind": "statute"},
    {"url": "https://pakistancode.gov.pk/english/LGu0xBD.php", "target_kind": "statute"},
    {"url": "https://pakistancode.gov.pk/english/LGu0xVD.php", "target_kind": "statute"},
]
PAKISTANCODE_DOCUMENT_PRIORITY = 10
PAKISTANCODE_LISTING_PRIORITY = 80
PAKISTANCODE_MIN_CRAWL_MAX_PAGES = 200


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    configured = cfg.get("listings") or cfg.get("statute_urls")
    if configured:
        out: List[Dict[str, Any]] = []
        for item in configured:
            if isinstance(item, dict) and item.get("url"):
                out.append({"url": item["url"], "target_kind": item.get("target_kind", "statute")})
            elif isinstance(item, str):
                out.append({"url": item, "target_kind": cfg.get("target_kind", "statute")})
        if out:
            return out
    return [dict(item) for item in DEFAULT_LISTINGS]


def _effective_crawl_limit(source: ScraperSource, explicit_limit: Optional[int]) -> int:
    if explicit_limit is not None:
        return max(1, int(explicit_limit))
    cfg_limit = (source.config_json or {}).get("crawl_max_pages")
    configured_limit = source.crawl_max_pages
    if cfg_limit is not None:
        try:
            configured_limit = int(cfg_limit)
        except (TypeError, ValueError):
            pass
    return max(PAKISTANCODE_MIN_CRAWL_MAX_PAGES, int(configured_limit or 0))


def normalize_pakistancode_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public PakistanCode hosts/routes."""
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
    path = parts.path or "/"
    if host in PAKISTANCODE_HOST_ALIASES:
        scheme = "https"
        netloc = PAKISTANCODE_HOST + (f":{parts.port}" if parts.port else "")
        if path.lower().startswith("/alpha/"):
            path = "/english/" + path.split("/", 2)[2]
        elif path.lower() == "/alpha":
            path = "/english/index.php"
        elif path == "/":
            path = "/english/index.php"
    path = quote(path, safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def _extract_viewer_pdf_url(url: str) -> Optional[str]:
    parts = urlsplit(url)
    if not VIEWER_PATH_RE.search((parts.path or "").lower()):
        return None
    fragment = parts.fragment or ""
    if not fragment:
        return None
    if "pdffiles/" not in fragment.lower():
        return None
    normalized_fragment = fragment.lstrip("./")
    if not normalized_fragment.startswith("/"):
        normalized_fragment = "/" + normalized_fragment
    while normalized_fragment.startswith("/../"):
        normalized_fragment = normalized_fragment[3:]
    if not PDF_PATH_RE.search(normalized_fragment):
        return None
    return urlunsplit((parts.scheme or "https", parts.netloc, quote(normalized_fragment, safe="/%:@,+;=()-.~_"), "", ""))


def _classify_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if PDF_PATH_RE.search(path):
        return "document"
    if DETAIL_PATH_RE.search(path) or LISTING_PATH_RE.search(path):
        return "listing"
    return None


def _infer_target_kind(*, title: str, source_section: str) -> str:
    lowered = (title or "").lower()
    if source_section in ("ordinances", "subordinate_legislation", "amendments"):
        return "instrument"
    if any(token in lowered for token in ("ordinance", "rules", "rule", "regulation", "order", "notification", "amendment")):
        return "instrument"
    return "statute"


class PakistanCodePipeline(PublicPipeline):
    """Source-specific extraction for PakistanCode listings/detail pages and pdffiles routing."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        default_target_kind = fr.query_json.get("target_kind", "statute")
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
            self._collect_listing_links(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
                inherited_meta={
                    "listing_fetch": "navigation_links",
                    "source_section": self._source_section_for_listing(res.final_url, tab_id=""),
                    "target_kind": default_target_kind,
                },
            )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=default_target_kind)
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
                    "detail_fetch",
                    "discovery_channel",
                    "result_index",
                    "source_section",
                    "category_title",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_type",
                    "act_promulgation_date",
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
                            "target_kind": nmeta.get("target_kind", default_target_kind),
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        # public_pipeline.pending() drains lower priorities first (ASC),
                        # so listing rows must stay behind document/PDF rows.
                        priority=PAKISTANCODE_LISTING_PRIORITY,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        path = (urlsplit(url).path or "").lower()
        return DETAIL_PATH_RE.search(path) is not None

    def _collect_structured_listing_rows(self, *, html_text: str, base_url: str, listings: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        listing_fetch = self._listing_fetch_for_url(base_url)
        tab_found = False

        for tab in soup.select("div.tab-pane[id]"):
            tab_id = (tab.get("id") or "").strip()
            if tab_id not in ("primary-legislation", "ordinance", "secondary-legislation", "amendment"):
                continue
            tab_found = True
            source_section = self._source_section_for_listing(base_url, tab_id=tab_id)
            for section in tab.select("div.accordion-section"):
                title_link = section.select_one(".accordion-section-title a[href]") or section.select_one("a[href]")
                if title_link is None:
                    continue
                row_index += 1
                title = self._clean_title(title_link.get_text(" ", strip=True))
                content_node = section.select_one(".accordion-section-content")
                content_text = content_node.get_text(" ", strip=True) if content_node else ""
                row_meta: Dict[str, Any] = {
                    "listing_fetch": listing_fetch,
                    "discovery_channel": "accordion-row",
                    "source_section": source_section,
                    "result_index": row_index,
                    "target_kind": _infer_target_kind(title=title, source_section=source_section),
                }
                if title:
                    row_meta["act_title"] = title[:280]
                self._add_row_provenance(row_meta=row_meta, content_text=content_text, title=title)
                self._capture_candidate(
                    raw=title_link.get("href", ""),
                    hint=title[:240],
                    base_url=base_url,
                    docs={},
                    listings=listings,
                    route_meta=row_meta,
                )

        if tab_found:
            return

        # Fallback for pages that expose direct encoded detail links outside tab panes.
        source_section = self._source_section_for_listing(base_url, tab_id="")
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "uy2fqajw1-" not in href.lower():
                continue
            row_index += 1
            title = self._clean_title(link.get_text(" ", strip=True))
            row_meta = {
                "listing_fetch": listing_fetch,
                "discovery_channel": "direct-listing-link",
                "source_section": source_section,
                "result_index": row_index,
                "target_kind": _infer_target_kind(title=title, source_section=source_section),
            }
            if title:
                row_meta["act_title"] = title[:280]
            self._add_row_provenance(row_meta=row_meta, content_text="", title=title)
            self._capture_candidate(
                raw=href,
                hint=title[:240],
                base_url=base_url,
                docs={},
                listings=listings,
                route_meta=row_meta,
            )

    def _collect_listing_links(
        self,
        *,
        html_text: str,
        base_url: str,
        listings: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs={},
                listings=listings,
                route_meta=inherited_meta,
            )

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h2") or soup.select_one("h1") or soup.select_one("title")
        detail_title = self._clean_title(detail_title_node.get_text(" ", strip=True)) if detail_title_node else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title and not base_meta.get("act_title"):
            base_meta["act_title"] = detail_title[:280]
        if detail_title:
            base_meta["detail_title"] = detail_title[:280]
            if "act_year" not in base_meta:
                year_match = YEAR_RE.search(detail_title)
                if year_match:
                    base_meta["act_year"] = year_match.group(0)
        base_meta.setdefault("target_kind", _infer_target_kind(title=detail_title, source_section=base_meta.get("source_section", "")))
        self._add_row_provenance(row_meta=base_meta, content_text=soup.get_text(" ", strip=True)[:1000], title=detail_title)

        for a in soup.find_all("a", href=True):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "download_tab_link"
            route_meta["discovery_channel"] = "detail-download-link"
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240] or detail_title[:240],
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

        for iframe in soup.find_all("iframe", src=True):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "viewer_iframe"
            route_meta["discovery_channel"] = "detail-viewer-iframe"
            self._capture_candidate(
                raw=iframe.get("src", ""),
                hint=(iframe.get("title", "") or detail_title)[:240],
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

        # Some detail views only expose the PDF in script literals.
        for match in re.findall(r"https?://[^\"' ]+/pdffiles/[^\"' ]+\.pdf", html_text or "", re.IGNORECASE):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "script_pdf_link"
            route_meta["discovery_channel"] = "detail-script-link"
            self._capture_candidate(raw=match, hint=detail_title[:240], base_url=base_url, docs=docs, listings={}, route_meta=route_meta)

    @staticmethod
    def _listing_fetch_for_url(url: str) -> str:
        path = (urlsplit(url).path or "").lower()
        if "lgu0xad" in path:
            return "alphabetical_accordion"
        if "lgu0xbd" in path:
            return "chronological_accordion"
        if "lgu0xvd" in path:
            return "category_accordion"
        return "homepage_links"

    @staticmethod
    def _source_section_for_listing(url: str, *, tab_id: str) -> str:
        if tab_id == "ordinance":
            return "ordinances"
        if tab_id == "secondary-legislation":
            return "subordinate_legislation"
        if tab_id == "amendment":
            return "amendments"
        parts = urlsplit(url)
        path = (parts.path or "").lower()
        action = (parse_qs(parts.query or "").get("action", [""])[0] or "").lower()
        if action in ("active", "nullactive", "null", "notactive"):
            return "ordinances"
        if "lgu0xbd" in path or "lgu0xad" in path or "lgu0xvd" in path:
            return "federal_laws"
        return "federal_laws"

    @staticmethod
    def _clean_title(value: str) -> str:
        return re.sub(r"^\s*\d+\.\s*", "", (value or "")).strip()

    def _add_row_provenance(self, *, row_meta: Dict[str, Any], content_text: str, title: str) -> None:
        text = " ".join((content_text or "").split())
        title = (title or "").strip()
        if text:
            first_segment = text.split("|", 1)[0].strip()
            if first_segment and first_segment.lower() != title.lower() and len(first_segment) <= 80:
                row_meta.setdefault("category_title", first_segment)
            act_no_match = ACT_NO_RE.search(text)
            if act_no_match:
                row_meta.setdefault("act_no", act_no_match.group(1)[:80])
            date_match = PROMULGATION_RE.search(text)
            if date_match:
                row_meta.setdefault("act_promulgation_date", date_match.group(1)[:40])
                if "act_year" not in row_meta and YEAR_RE.search(date_match.group(1)):
                    row_meta["act_year"] = YEAR_RE.search(date_match.group(1)).group(0)  # type: ignore[union-attr]
        for candidate in (title, row_meta.get("act_no", ""), row_meta.get("act_promulgation_date", "")):
            if candidate and "act_year" not in row_meta:
                year_match = YEAR_RE.search(str(candidate))
                if year_match:
                    row_meta["act_year"] = year_match.group(0)

        lowered = title.lower()
        source_section = row_meta.get("source_section", "")
        if source_section == "ordinances" or "ordinance" in lowered:
            row_meta.setdefault("act_type", "ordinance")
        elif "rules" in lowered or " rule" in lowered:
            row_meta.setdefault("act_type", "rules")
        elif "regulation" in lowered:
            row_meta.setdefault("act_type", "regulation")
        elif "notification" in lowered:
            row_meta.setdefault("act_type", "notification")
        elif "act" in lowered:
            row_meta.setdefault("act_type", "act")

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
        endpoint_kind: Optional[str] = None
        joined_raw = html.unescape(str(raw or "")).strip()
        if joined_raw:
            if not joined_raw.lower().startswith(("http://", "https://")):
                joined_raw = urljoin(base_url, joined_raw)
            viewer_pdf = _extract_viewer_pdf_url(joined_raw)
            if viewer_pdf:
                raw = viewer_pdf
                endpoint_kind = "viewerjs-pdf-embed"

        normalized = normalize_pakistancode_public_url(raw, base_url=base_url)
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
                "pdf_endpoint_kind": endpoint_kind or ("pdffiles-direct" if path.startswith("/pdffiles/") else "direct-file"),
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
            return

        if kind == "listing" and safe != base_url:
            existing = listings.get(safe)
            if existing is None:
                listings[safe] = dict(route_meta)
                if self._is_detail_listing(safe):
                    listings[safe].setdefault("detail_url", safe)
                    listings[safe].setdefault(
                        "target_kind",
                        _infer_target_kind(
                            title=listings[safe].get("act_title", ""),
                            source_section=listings[safe].get("source_section", ""),
                        ),
                    )
            else:
                for key, value in route_meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value

    async def _enqueue_documents_with_meta(
        self,
        docs: Dict[str, Dict[str, Any]],
        *,
        listing_url: str,
        default_target_kind: str,
    ) -> int:
        added = 0
        for url, meta in docs.items():
            kind = meta.get("target_kind") or default_target_kind
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
                "category_title",
                "act_year",
                "act_no",
                "act_title",
                "act_type",
                "act_promulgation_date",
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
                    priority=PAKISTANCODE_DOCUMENT_PRIORITY,
                )
            )
            added += 1
        await self.db.flush()
        return added

    async def _drain_one(self, fr: CrawlFrontier) -> str:
        """Process one frontier row with fail-closed `%PDF` checks on PDF document candidates."""
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
            elif res.status_code >= 400:
                fr.status = "retired" if res.status_code in (404, 410) else "pending"
                fr.last_error = f"HTTP {res.status_code}"
            else:
                if kind in ("statute", "instrument"):
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


async def scrape_pakistancode(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    opts = dict(kwargs)
    opts["limit"] = _effective_crawl_limit(source, opts.get("limit"))
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=PakistanCodePipeline,
        **opts,
    )
