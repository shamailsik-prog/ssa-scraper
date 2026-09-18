"""
NasirLawSite — PUBLIC case-law and statute source.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by shared pipeline guards
  - listing pages fan out into detail pages, then into document candidates
  - detail pages enqueue their own HTML as a document plus direct PDF links
  - when a candidate is marked expect_pdf it must contain `%PDF`, else retire
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
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

logger = logging.getLogger(__name__)

NASIR_HOST = "www.nasirlawsite.com"
NASIR_HOST_ALIASES = (NASIR_HOST, "nasirlawsite.com")
REPORTER_HINT_RE = re.compile(r"\b(PLD|SCMR|YLR|MLD|CLC|PLJ|PCrLJ|PCRLJ|PTD|PTCL|CLD|CIML)\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
PDF_PATH_RE = re.compile(r"(?i)\.pdf$")
LEGACY_REPORTER_LISTING_PATH_RE = re.compile(r"(?i)^/case/[a-z0-9_-]+\.htm$")
HISTORIC_ROOT_LISTING_PATH_RE = re.compile(r"(?i)^/historic(?:\.htm)?/?$")
HISTORIC_DETAIL_PATH_RE = re.compile(r"(?i)^/historic/[a-z0-9_-]+\.htm$")
LAWS_ROOT_LISTING_PATH_RE = re.compile(r"(?i)^/laws(?:\.htm|/)?$")
LAWS_DETAIL_PATH_RE = re.compile(r"(?i)^/laws/[a-z0-9_-]+\.htm$")
ROOT_LISTING_PATH_RE = re.compile(r"(?i)^/(?:|index\.html?)$")

DEFAULT_LISTINGS: List[Dict[str, str]] = [
    {"url": "https://www.nasirlawsite.com/index.html", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/historic.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/scmr.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/pld.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/clc.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/ylr.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/case/mld.htm", "target_kind": "judgment"},
    {"url": "https://www.nasirlawsite.com/laws.htm", "target_kind": "statute"},
]


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    configured = cfg.get("listings")
    if configured:
        out: List[Dict[str, Any]] = []
        for item in configured:
            if isinstance(item, dict) and item.get("url"):
                out.append({"url": item["url"], "target_kind": item.get("target_kind", "judgment")})
            elif isinstance(item, str):
                out.append({"url": item, "target_kind": cfg.get("target_kind", "judgment")})
        if out:
            return out
    return [dict(item) for item in DEFAULT_LISTINGS]


def normalize_nasirlaw_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered URLs onto official public NasirLawSite hosts/routes."""
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
    if host in NASIR_HOST_ALIASES:
        scheme = "https"
        netloc = NASIR_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def _classify_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if PDF_PATH_RE.search(path):
        return "document"
    if HISTORIC_DETAIL_PATH_RE.search(path) or LAWS_DETAIL_PATH_RE.search(path):
        return "detail_listing"
    if (
        ROOT_LISTING_PATH_RE.search(path)
        or HISTORIC_ROOT_LISTING_PATH_RE.search(path)
        or LAWS_ROOT_LISTING_PATH_RE.search(path)
        or LEGACY_REPORTER_LISTING_PATH_RE.search(path)
    ):
        return "listing"
    return None


def _infer_statute_target_kind(*, title: str, source_section: str) -> str:
    lowered = (title or "").lower()
    if source_section in ("laws_rules", "laws_ordinances", "laws_orders"):
        return "instrument"
    if any(token in lowered for token in ("ordinance", "rules", "rule", "regulation", "order", "notification", "amendment", "bill")):
        return "instrument"
    return "statute"


def _source_section_for_url(url: str) -> str:
    path = (urlsplit(url).path or "").lower()
    if HISTORIC_ROOT_LISTING_PATH_RE.search(path) or HISTORIC_DETAIL_PATH_RE.search(path):
        return "historic_decisions"
    if LAWS_ROOT_LISTING_PATH_RE.search(path) or LAWS_DETAIL_PATH_RE.search(path):
        return "laws_index"
    if LEGACY_REPORTER_LISTING_PATH_RE.search(path):
        return "reporter_index"
    return "site_navigation"


class NasirLawSitePipeline(PublicPipeline):
    """Public listing/detail/document discovery for NasirLawSite case-law and statutes."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        default_target_kind = fr.query_json.get("target_kind", "judgment")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_detail_listing(res.final_url):
            self._collect_detail_document_links(
                html_text=res.text,
                detail_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
                default_target_kind=default_target_kind,
            )
        else:
            self._collect_listing_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
                inherited_meta={
                    **inherited_meta,
                    "listing_fetch": self._listing_fetch_for_url(res.final_url),
                    "source_section": _source_section_for_url(res.final_url),
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
                    "reporter_hint",
                    "citation_hint",
                    "act_title",
                    "act_year",
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
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        path = (urlsplit(url).path or "").lower()
        return HISTORIC_DETAIL_PATH_RE.search(path) is not None or LAWS_DETAIL_PATH_RE.search(path) is not None

    @staticmethod
    def _listing_fetch_for_url(url: str) -> str:
        path = (urlsplit(url).path or "").lower()
        if HISTORIC_ROOT_LISTING_PATH_RE.search(path):
            return "historic_index"
        if LAWS_ROOT_LISTING_PATH_RE.search(path):
            return "laws_index"
        if LEGACY_REPORTER_LISTING_PATH_RE.search(path):
            return "reporter_index"
        return "site_navigation"

    def _collect_listing_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for idx, a in enumerate(soup.find_all("a", href=True), start=1):
            hint = " ".join((a.get_text(" ", strip=True) or "").split())
            route_meta = dict(inherited_meta)
            route_meta["result_index"] = idx
            route_meta["discovery_channel"] = "listing-anchor"
            self._add_row_metadata(route_meta=route_meta, title=hint)
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=hint[:280],
                base_url=base_url,
                docs=docs,
                listings=listings,
                route_meta=route_meta,
            )

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        detail_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
        default_target_kind: str,
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("title")
        detail_title = " ".join((detail_title_node.get_text(" ", strip=True) if detail_title_node else "").split())

        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", detail_url)
        base_meta.setdefault("source_section", _source_section_for_url(detail_url))
        inferred_kind = self._target_kind_for_detail(
            detail_url=detail_url,
            hint=detail_title,
            default_target_kind=base_meta.get("target_kind", default_target_kind),
            source_section=base_meta.get("source_section", ""),
        )
        base_meta["target_kind"] = inferred_kind
        if detail_title:
            if inferred_kind in ("statute", "instrument"):
                base_meta.setdefault("act_title", detail_title[:280])
            else:
                base_meta.setdefault("case_title", detail_title[:280])
        self._add_row_metadata(route_meta=base_meta, title=detail_title)

        self._merge_doc(
            docs,
            detail_url,
            {
                "detail_fetch": "html_detail_page",
                "discovery_channel": "detail-page-self",
                "document_format": "html",
                **base_meta,
            },
        )

        for a in soup.find_all("a", href=True):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "detail-anchor-link"
            route_meta["discovery_channel"] = "detail-anchor"
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:280] or detail_title[:280],
                base_url=detail_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

        for iframe in soup.find_all("iframe", src=True):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "detail-iframe"
            route_meta["discovery_channel"] = "detail-iframe"
            self._capture_candidate(
                raw=iframe.get("src", ""),
                hint=(iframe.get("title", "") or detail_title)[:280],
                base_url=detail_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

        for match in re.findall(r"https?://[^\"' ]+\.pdf(?:\?[^\"' ]*)?", html_text or "", re.IGNORECASE):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "detail-script-link"
            route_meta["discovery_channel"] = "detail-script-link"
            self._capture_candidate(
                raw=match,
                hint=detail_title[:280],
                base_url=detail_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    def _add_row_metadata(self, *, route_meta: Dict[str, Any], title: str) -> None:
        text = (title or "").strip()
        if not text:
            return
        reporter = REPORTER_HINT_RE.search(text)
        if reporter:
            route_meta.setdefault("reporter_hint", reporter.group(1).upper())
            route_meta.setdefault("citation_hint", text[:280])
        year_match = YEAR_RE.search(text)
        if year_match:
            route_meta.setdefault("act_year", year_match.group(0))
        target_kind = route_meta.get("target_kind")
        if target_kind in ("statute", "instrument"):
            route_meta.setdefault("act_title", text[:280])
            route_meta["target_kind"] = _infer_statute_target_kind(title=text, source_section=route_meta.get("source_section", ""))

    def _target_kind_for_detail(self, *, detail_url: str, hint: str, default_target_kind: str, source_section: str) -> str:
        path = (urlsplit(detail_url).path or "").lower()
        if HISTORIC_ROOT_LISTING_PATH_RE.search(path) or LEGACY_REPORTER_LISTING_PATH_RE.search(path):
            return "judgment"
        if LAWS_ROOT_LISTING_PATH_RE.search(path):
            return _infer_statute_target_kind(title=hint, source_section=source_section)
        if HISTORIC_DETAIL_PATH_RE.search(path):
            return "judgment"
        if LAWS_DETAIL_PATH_RE.search(path):
            return _infer_statute_target_kind(title=hint, source_section=source_section)
        return default_target_kind

    def _merge_doc(self, docs: Dict[str, Dict[str, Any]], url: str, meta: Dict[str, Any]) -> None:
        existing = docs.get(url)
        if existing is None:
            docs[url] = dict(meta)
            return
        for key, value in meta.items():
            if key not in existing and value not in ("", None):
                existing[key] = value

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
        normalized = normalize_nasirlaw_public_url(raw, base_url=base_url)
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
                "discovery_hint": hint[:280],
                "pdf_endpoint_kind": "direct-file",
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            self._merge_doc(docs, safe, meta)
            return

        if kind in ("listing", "detail_listing") and safe != base_url:
            next_meta = dict(route_meta)
            if kind == "detail_listing":
                next_meta.setdefault("detail_url", safe)
            next_meta.setdefault("source_section", _source_section_for_url(safe))
            next_meta["target_kind"] = self._target_kind_for_detail(
                detail_url=safe,
                hint=hint,
                default_target_kind=route_meta.get("target_kind", "judgment"),
                source_section=next_meta.get("source_section", ""),
            )
            self._add_row_metadata(route_meta=next_meta, title=hint)
            existing = listings.get(safe)
            if existing is None:
                listings[safe] = next_meta
            else:
                for key, value in next_meta.items():
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
                "reporter_hint",
                "citation_hint",
                "act_title",
                "act_year",
                "detail_url",
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
        """Process one frontier row with fail-closed `%PDF` checks for expect_pdf candidates."""
        fr.status = "in_progress"
        fr.attempts += 1
        kind = fr.query_json.get("kind", "judgment")
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


async def scrape_nasirlawsite(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=NasirLawSitePipeline,
        **kwargs,
    )
