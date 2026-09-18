"""
Balochistan High Court (PUBLIC judgments) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - BHC result boxes are harvested for source-specific metadata
  - candidate links are normalized onto public BHC hosts
  - judgment document links must pass the `%PDF` gate before ingestion
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

PUBLIC_HOST = "bhc.gov.pk"
PUBLIC_HOST_ALIASES = (
    PUBLIC_HOST,
    "www.bhc.gov.pk",
)

DEFAULT_LISTINGS = [
    "https://bhc.gov.pk/resources/judgments",
    "https://bhc.gov.pk/judgments",
]

DOC_HINT_RE = re.compile(r"(?i)(judg|judgement|judgment|order|case|appeal|petition|pld|mld|ylr|vs\\b|v\\.)")
WAYBACK_RE = re.compile(r"/web/\\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?media/judgments/[^\"'<>\\s]+?\\.pdf(?:\\?[^\"'<>\\s]*)?)")
REL_LISTING_RE = re.compile(r"(?i)(/?(?:ur/)?resources/judgments(?:/[^\"'<>\\s]*)?)")
ROOT_LISTING_RE = re.compile(r"(?i)(/?(?:ur/)?judgments(?:/[^\"'<>\\s]*)?)")

LISTING_PATH_RE = re.compile(r"(?i)^/(?:ur/)?resources/judgments(?:/.*)?/?$")
ROOT_LISTING_PATH_RE = re.compile(r"(?i)^/(?:ur/)?judgments/?$")
DOC_PATH_RE = re.compile(r"(?i)^/media/judgments/.+\\.pdf$")
YEAR_RE = re.compile(r"\\b(19|20)\\d{2}\\b")


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("listings") or DEFAULT_LISTINGS
    return [{"url": u, "target_kind": "judgment"} for u in urls]


def normalize_bhc_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates to official BHC public URL form."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    candidate = _unwrap_wayback(candidate)
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    if re.match(r"(?i)^media/judgments/", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^(?:ur/)?resources/judgments", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^(?:ur/)?judgments(?:$|\\?)", candidate):
        candidate = "/" + candidate

    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PUBLIC_HOST_ALIASES:
        scheme = "https"
        netloc = PUBLIC_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def _unwrap_wayback(url: str) -> str:
    m = WAYBACK_RE.search(url)
    if m:
        return m.group(1)
    return url


def _iter_discovery_candidates(html_text: str) -> Iterable[Tuple[str, str, str]]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for a in soup.find_all("a", href=True):
        yield (a.get("href", ""), "anchor", a.get_text(" ", strip=True))
    for tag in soup.find_all(True):
        for attr, value in (tag.attrs or {}).items():
            if not str(attr).startswith("data-"):
                continue
            values = value if isinstance(value, list) else [value]
            for v in values:
                if not isinstance(v, str):
                    continue
                for candidate in _extract_embedded_candidates(v):
                    yield (candidate, "data-attr", f"{attr}:{v[:120]}")
    for script in soup.find_all("script"):
        body = script.get_text(" ", strip=False) or ""
        for candidate in _extract_embedded_candidates(body):
            yield (candidate, "inline-script", candidate[:120])


def _extract_embedded_candidates(blob: str) -> Iterable[str]:
    text = html.unescape(blob or "")
    for u in ABS_URL_RE.findall(text):
        yield u.rstrip(");,")
    for u in REL_DOC_RE.findall(text):
        yield u.rstrip(");,")
    for u in REL_LISTING_RE.findall(text):
        yield u.rstrip(");,")
    for u in ROOT_LISTING_RE.findall(text):
        yield u.rstrip(");,")


def _is_judgment_pdf(url: str, *, hint_text: str) -> bool:
    parts = urlsplit(url)
    path = parts.path or "/"
    low_path = path.lower()
    if not low_path.endswith(".pdf"):
        return False
    if DOC_PATH_RE.search(path):
        return True
    return bool(DOC_HINT_RE.search(hint_text or ""))


def _classify_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    path = urlsplit(url).path or "/"
    if _is_judgment_pdf(url, hint_text=hint_text):
        return "judgment"
    if LISTING_PATH_RE.search(path) or ROOT_LISTING_PATH_RE.search(path):
        return "listing"
    return None


def _looks_like_bhc_result_listing(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower()
    if "/resources/judgments/" not in path:
        return False
    low = (html_text or "").lower()
    return "judgmentbox" in low and "data-src" in low and "/media/judgments/" in low


class BalochistanHighCourtPipeline(PublicPipeline):
    """Public pipeline with BHC result-box and direct-PDF discovery."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        docs: Dict[str, Dict[str, Any]] = {}
        listings: List[str] = []

        self._collect_from_html(
            html_text=res.text,
            base_url=res.final_url,
            docs=docs,
            listings=listings,
        )

        if _looks_like_bhc_result_listing(res.final_url, res.text):
            self._collect_result_boxes(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
            )

        added = await self._enqueue_judgments_with_meta(docs, listing_url=res.final_url)
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
                        query_json={"kind": "listing", "url": nurl, "target_kind": "judgment", "depth": depth + 1},
                        cursor_json={},
                        priority=40,
                    )
                )
        await self.db.flush()

    def _collect_result_boxes(self, *, html_text: str, base_url: str, docs: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for idx, box in enumerate(soup.select("div.judgmentbox"), start=1):
            serial = (box.select_one(".serial").get_text(" ", strip=True) if box.select_one(".serial") else "")[:50]
            title = (box.select_one(".title").get_text(" ", strip=True) if box.select_one(".title") else "")[:200]
            citation = (box.select_one(".note").get_text(" ", strip=True) if box.select_one(".note") else "")[:140]
            year = None
            for fragment in (citation, title, urlsplit(base_url).path):
                m = YEAR_RE.search(fragment or "")
                if m:
                    year = m.group(0)
                    break
            row_meta: Dict[str, Any] = {
                "listing_fetch": "result_box",
                "result_index": idx,
            }
            if serial:
                row_meta["result_serial"] = serial
            if title:
                row_meta["result_title"] = title
            if citation:
                row_meta["result_citation"] = citation
            if year:
                row_meta["result_year"] = year

            for tag in box.find_all(True):
                for attr in ("data-src", "href"):
                    raw = tag.get(attr)
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    self._capture_candidate(
                        raw=raw,
                        hint=f"result-box:{title or citation or serial}",
                        channel="result-box",
                        base_url=base_url,
                        docs=docs,
                        listings=[],
                        route_meta=row_meta,
                    )

    def _collect_from_html(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        route_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        route_meta = route_meta or {}
        for raw, channel, hint in _iter_discovery_candidates(html_text):
            self._capture_candidate(
                raw=raw,
                hint=hint,
                channel=channel,
                base_url=base_url,
                docs=docs,
                listings=listings,
                route_meta=route_meta,
            )

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        channel: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_bhc_public_url(raw, base_url=base_url)
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
        kind = _classify_discovered_url(safe, hint_text=hint)
        if kind == "judgment":
            meta = {
                "discovery_channel": channel,
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "media-judgments",
                **route_meta,
            }
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            listings.append(safe)

    async def _enqueue_judgments_with_meta(self, docs: Dict[str, Dict[str, Any]], *, listing_url: str) -> int:
        added = 0
        for url, meta in docs.items():
            key = f"judgment:{url}"
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
                "result_serial",
                "result_title",
                "result_citation",
                "result_year",
                "pdf_endpoint_kind",
            ):
                if key_name in meta:
                    route[key_name] = meta[key_name]
            self.db.add(
                CrawlFrontier(
                    source_name=self.source.source_name,
                    tier=0,
                    query_key=key,
                    query_json={"kind": "judgment", "url": url, "route": route, "meta": meta},
                    cursor_json={},
                    priority=50,
                )
            )
            added += 1
        await self.db.flush()
        return added


async def scrape_balochistan_high_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=BalochistanHighCourtPipeline,
        **kwargs,
    )
