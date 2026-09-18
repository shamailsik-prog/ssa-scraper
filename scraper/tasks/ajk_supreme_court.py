"""
Azad Jammu & Kashmir Supreme Court (PUBLIC judgments/orders) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - candidate links are normalized onto the AJK Supreme Court public domains
  - judgment document links must pass the `%PDF` gate before ingestion
"""

from __future__ import annotations

import html
import re
from itertools import islice
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

PUBLIC_HOST = "ajksupremecourt.gok.pk"
SCAPP_HOST = "scapp.ajksupremecourt.gok.pk"
PUBLIC_HOST_ALIASES = (PUBLIC_HOST, f"www.{PUBLIC_HOST}")

DEFAULT_LISTINGS = [
    "https://ajksupremecourt.gok.pk/judgements-orders/",
    "https://ajksupremecourt.gok.pk/category/judgments/",
    "https://scapp.ajksupremecourt.gok.pk/Judgements.php",
]
DEFAULT_RESULT_PAGE_SIZE = 200
DEFAULT_RESULT_MAX_PAGES = 2

DOC_HINT_RE = re.compile(r"(?i)(judg|order|appeal|petition|case|vs\.?|v\.?\s)")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?wp-content/uploads/[^\"'<>]+)")
REL_LISTING_RE = re.compile(
    r"(?i)(/?(?:category/(?:judgments?|judgements?|orders?)(?:/page/\d+)?/?|judgements-orders/?|Judgements\.php(?:\?[^\"'<>\s]*)?))"
)
LISTING_PATH_RE = re.compile(
    r"(?i)^/(category/(?:judgments?|judgements?|orders?)(?:/page/\d+)?/?|judgements-orders/?|Judgements\.php)$"
)
WP_ARCHIVE_PATH_RE = re.compile(r"(?i)^/(category/(?:judgments?|judgements?)(?:/page/\d+)?/?|judgements-orders/?)$")
WP_ARCHIVE_PAGE_RE = re.compile(r"(?i)^/category/(?:judgments?|judgements?)/page/(\d+)/?$")


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("listings") or DEFAULT_LISTINGS
    return [{"url": u, "target_kind": "judgment"} for u in urls]


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


def result_window_for(source: ScraperSource) -> Tuple[int, int]:
    cfg = source.config_json or {}
    page_size = _positive_int(cfg.get("result_page_size"), default=DEFAULT_RESULT_PAGE_SIZE)
    max_pages = _positive_int(cfg.get("result_max_pages"), default=DEFAULT_RESULT_MAX_PAGES)
    return (max(1, page_size), max(1, max_pages))


def _windowed(items: Sequence[Any], *, page_size: int, max_pages: int) -> Iterator[Tuple[int, int, Any]]:
    limit = page_size * max_pages
    for idx, item in enumerate(islice(items, limit)):
        yield ((idx // page_size) + 1, idx % page_size, item)


def _clean_text(text: str, *, limit: int = 260) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _looks_like_wp_archive_listing(url: str, html_text: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = (parts.path or "/").lower()
    if host not in PUBLIC_HOST_ALIASES:
        return False
    if not WP_ARCHIVE_PATH_RE.search(path):
        return False
    low = (html_text or "").lower()
    return '<div id="news"' in low and "archive for judgments" in low


def _wp_archive_listing_page(url: str) -> Optional[int]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in PUBLIC_HOST_ALIASES:
        return None
    path = (parts.path or "/").lower()
    m = WP_ARCHIVE_PAGE_RE.search(path)
    if m:
        return max(1, int(m.group(1)))
    if path.rstrip("/") in ("/category/judgments", "/category/judgements", "/judgements-orders"):
        return 1
    return None


def _archive_rows(html_text: str) -> List[Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    container = soup.find("div", id="news")
    if container is None:
        return []
    rows: List[Any] = []
    for tag in container.find_all("div"):
        classes = [str(c).lower() for c in (tag.get("class") or [])]
        if "news" in classes and "wide" in classes and tag.find("a", href=True):
            rows.append(tag)
    return rows


def normalize_ajk_supreme_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize a discovered candidate to AJK Supreme Court public-domain URL form."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    candidate = _unwrap_wayback(candidate)
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    if re.match(r"(?i)^wp-content/uploads/", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^(category/(?:judgments?|judgements?|orders?)(?:/page/\d+)?/?|judgements-orders/?|Judgements\.php(?:\?.*)?)$", candidate):
        candidate = "/" + candidate
    if candidate.lower().startswith(("http://", "https://")):
        joined = candidate
    else:
        joined = urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    netloc = parts.netloc
    scheme = parts.scheme or "https"
    if host in PUBLIC_HOST_ALIASES:
        scheme = "https"
        netloc = PUBLIC_HOST + (f":{parts.port}" if parts.port else "")
    elif host == SCAPP_HOST:
        scheme = "https"
        netloc = SCAPP_HOST + (f":{parts.port}" if parts.port else "")
    path = parts.path or "/"
    path = quote(path, safe="/%:@,+;=()-.~_")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _unwrap_wayback(url: str) -> str:
    m = WAYBACK_RE.search(url)
    if m:
        return m.group(1)
    return url


def _iter_discovery_candidates(html_text: str) -> Iterable[Tuple[str, str, str]]:
    """
    Yield (raw_candidate, channel, hint_text) from anchors, data-* attributes, and scripts.
    `hint_text` is used to keep only judgment/order-related links.
    """
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


def _classify_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    low_path = path.lower()
    query = (parts.query or "").lower()
    hint = (hint_text or "").lower()
    is_pdf = low_path.endswith(".pdf") or ".pdf" in low_path
    in_uploads = "/wp-content/uploads/" in low_path

    if is_pdf and (in_uploads or DOC_HINT_RE.search(low_path) or DOC_HINT_RE.search(hint)):
        return "judgment"
    if _looks_like_judgment_post(low_path, hint):
        return "judgment"
    if LISTING_PATH_RE.search(path) or ("judgements.php" in low_path and "page=" in query):
        return "listing"
    if host == SCAPP_HOST and "judgements.php" in low_path:
        return "listing"
    return None


def _looks_like_judgment_post(path: str, hint: str) -> bool:
    # AJK SC WordPress judgment posts are root slugs (for example /fareeda-rafique-vs-.../).
    if path in ("", "/"):
        return False
    if path.startswith(("/category/", "/tag/", "/author/", "/wp-", "/feed", "/comments", "/xmlrpc.php")):
        return False
    segments = [s for s in path.split("/") if s]
    if len(segments) != 1:
        return False
    slug = segments[0]
    if not re.match(r"^[a-z0-9][a-z0-9-]{5,}$", slug):
        return False
    if any(token in slug for token in ("vs", "judgment", "judgement", "order", "appeal", "petition")):
        return True
    return bool(DOC_HINT_RE.search(hint) or ".pdf" in hint or "supreme court of aj" in hint)


class AJKSupremeCourtPipeline(PublicPipeline):
    """Public pipeline with AJK Supreme Court robust listing and post discovery."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        docs: Dict[str, Dict[str, Any]] = {}
        listings: List[str] = []
        saw_archive_rows = False
        is_wp_archive_listing = _looks_like_wp_archive_listing(res.final_url, res.text)

        if is_wp_archive_listing:
            saw_archive_rows = self._collect_archive_rows_docs(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
            )

        self._collect_from_html(
            html_text=res.text,
            base_url=res.final_url,
            docs=docs,
            listings=listings,
            allow_judgment_capture=not saw_archive_rows,
        )

        added = await self._enqueue_judgments_with_meta(docs, listing_url=res.final_url)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        _, window_max_pages = result_window_for(self.source)
        for nurl in list(dict.fromkeys(listings)):
            if is_wp_archive_listing:
                listing_page = _wp_archive_listing_page(nurl)
                if listing_page is not None and listing_page > window_max_pages:
                    continue
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

    def _collect_from_html(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        route_meta: Optional[Dict[str, Any]] = None,
        allow_judgment_capture: bool = True,
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
                allow_judgment_capture=allow_judgment_capture,
            )

    def _collect_archive_rows_docs(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
    ) -> bool:
        rows = _archive_rows(html_text)
        if not rows:
            return False
        page_size, max_pages = result_window_for(self.source)
        listing_page = _wp_archive_listing_page(base_url)
        for page, page_index, row in _windowed(rows, page_size=page_size, max_pages=max_pages):
            title_anchor = row.select_one("h3 a[href]")
            title = _clean_text(title_anchor.get_text(" ", strip=True), limit=260) if title_anchor else ""
            posted = _clean_text((row.select_one("div.newsInfo") or row).get_text(" ", strip=True), limit=80)
            row_meta: Dict[str, Any] = {
                "listing_fetch": "archive_rows",
                "result_window_page": page,
                "result_window_index": page_index,
                "result_window_page_size": page_size,
                "result_window_max_pages": max_pages,
            }
            if listing_page is not None:
                row_meta["result_listing_page"] = listing_page
            if title:
                row_meta["result_title"] = title
            if posted:
                row_meta["result_posted_date"] = posted

            anchors = []
            if title_anchor is not None:
                anchors.append(title_anchor)
            for anchor in row.find_all("a", href=True):
                if title_anchor is not None and anchor is title_anchor:
                    continue
                href = (anchor.get("href") or "").lower()
                label = _clean_text(anchor.get_text(" ", strip=True), limit=120).lower()
                classes = " ".join(anchor.get("class", [])).lower()
                if ".pdf" in href or "drive.google.com" in href or "download" in label or "download" in classes:
                    anchors.append(anchor)

            seen_hrefs = set()
            for anchor in anchors:
                href = anchor.get("href") or ""
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                self._capture_candidate(
                    raw=href,
                    hint=f"archive-row:{title or posted}",
                    channel="archive-row",
                    base_url=base_url,
                    docs=docs,
                    listings=listings,
                    route_meta=row_meta,
                )
        return True

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
        allow_judgment_capture: bool = True,
    ) -> None:
        normalized = normalize_ajk_supreme_public_url(raw, base_url=base_url)
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
        if kind == "judgment" and allow_judgment_capture:
            meta = {
                "discovery_channel": channel,
                "discovery_hint": hint[:240],
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
                "result_window_page",
                "result_window_index",
                "result_window_page_size",
                "result_window_max_pages",
                "result_listing_page",
                "result_title",
                "result_posted_date",
            ):
                if key_name in meta:
                    route[key_name] = meta[key_name]
            self.db.add(
                CrawlFrontier(
                    source_name=self.source.source_name,
                    tier=0,
                    query_key=key,
                    query_json={
                        "kind": "judgment",
                        "url": url,
                        "route": route,
                        "meta": meta,
                    },
                    cursor_json={},
                    priority=50,
                )
            )
            added += 1
        await self.db.flush()
        return added


async def scrape_ajk_supreme_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=AJKSupremeCourtPipeline,
        **kwargs,
    )
