"""
Sindh High Court (PUBLIC judgments) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - SHC result grids are harvested from official public caselaw listings
  - detail rows are windowed into explicit result pages (bounded)
  - direct file-view/download endpoints are normalized onto public SHC hosts
  - judgment document links must pass the `%PDF` gate before ingestion
"""

from __future__ import annotations

import html
import re
from itertools import islice
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

PUBLIC_HOST = "caselaw.shc.gov.pk"
PUBLIC_HOST_ALIASES = (
    PUBLIC_HOST,
    "www.caselaw.shc.gov.pk",
    "www.shc.gov.pk",
    "shc.gov.pk",
    "sindhhighcourt.gov.pk",
    "www.sindhhighcourt.gov.pk",
)

DEFAULT_LISTINGS = [
    "https://caselaw.shc.gov.pk/caselaw/public/home",
    "https://caselaw.shc.gov.pk/caselaw/public/rpt-afr",
]

DEFAULT_DETAIL_RESULT_PAGE_SIZE = 200
DEFAULT_DETAIL_RESULT_MAX_PAGES = 2
DEFAULT_REPORT_RESULT_PAGE_SIZE = 40
DEFAULT_REPORT_RESULT_MAX_PAGES = 2

DOC_HINT_RE = re.compile(r"(?i)(judg|judgement|judgment|order|appeal|petition|case|citation)")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_VIEW_RE = re.compile(r"(?i)(/?(?:caselaw/)?(?:public/)?view-file/[A-Za-z0-9%_+=-]+)")
REL_DOWNLOAD_RE = re.compile(r"(?i)(/?(?:caselaw/)?download-file\.php\?doc=[^\"'<>\s]+(?:&(?:amp;)?citation=[^\"'<>\s]*)?)")
REL_DETAIL_RE = re.compile(r"(?i)(/?(?:caselaw/)?public/reported-judgements-detail-all/\d+/(?:-1|AFR|[^\"'<>\s]+))")
REL_REPORT_RE = re.compile(r"(?i)(/?(?:caselaw/)?public/rpt-afr(?:\?[^\"'<>\s]*)?)")

LISTING_PATH_RE = re.compile(r"(?i)^/caselaw/public/(home|rpt-afr)/?$")
DETAIL_LISTING_PATH_RE = re.compile(r"(?i)^/caselaw/public/reported-judgements-detail-all/\d+/(?:-1|afr|[^/]+)$")
VIEW_FILE_PATH_RE = re.compile(r"(?i)^/caselaw/view-file/[A-Za-z0-9%_+=-]+/?$")
DOWNLOAD_FILE_PATH_RE = re.compile(r"(?i)^/caselaw/download-file\.php$")


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("listings") or DEFAULT_LISTINGS
    return [{"url": u, "target_kind": "judgment"} for u in urls]


def normalize_shc_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates to public SHC caselaw URL form."""
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
    if re.match(r"(?i)^view-file/", candidate):
        candidate = "/caselaw/" + candidate
    if re.match(r"(?i)^download-file\.php\?doc=", candidate):
        candidate = "/caselaw/" + candidate
    if re.match(r"(?i)^public/(home|rpt-afr|reported-judgements-detail-all/)", candidate):
        candidate = "/caselaw/" + candidate
    if re.match(r"(?i)^reported-judgements-detail-all/", candidate):
        candidate = "/caselaw/public/" + candidate
    if re.match(r"(?i)^caselaw/(view-file|download-file\.php|public/)", candidate):
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
    query_items = parse_qs(parts.query or "", keep_blank_values=True)
    query = urlencode(query_items, doseq=True, quote_via=quote, safe="/:,+%=")
    return urlunsplit((scheme, netloc, path, query, ""))


def _unwrap_wayback(url: str) -> str:
    m = WAYBACK_RE.search(url)
    if m:
        return m.group(1)
    return url


def detail_result_window_for(source: ScraperSource) -> Tuple[int, int]:
    cfg = source.config_json or {}
    page_size = _positive_int(cfg.get("detail_result_page_size"), default=DEFAULT_DETAIL_RESULT_PAGE_SIZE)
    max_pages = _positive_int(cfg.get("detail_result_max_pages"), default=DEFAULT_DETAIL_RESULT_MAX_PAGES)
    return (max(1, page_size), max(1, max_pages))


def report_result_window_for(source: ScraperSource) -> Tuple[int, int]:
    cfg = source.config_json or {}
    page_size = _positive_int(cfg.get("report_result_page_size"), default=DEFAULT_REPORT_RESULT_PAGE_SIZE)
    max_pages = _positive_int(cfg.get("report_result_max_pages"), default=DEFAULT_REPORT_RESULT_MAX_PAGES)
    return (max(1, page_size), max(1, max_pages))


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


def _windowed(items: Sequence[Any], *, page_size: int, max_pages: int) -> Iterator[Tuple[int, int, Any]]:
    limit = page_size * max_pages
    for idx, item in enumerate(islice(items, limit)):
        yield ((idx // page_size) + 1, idx % page_size, item)


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
    for u in REL_VIEW_RE.findall(text):
        yield u.rstrip(");,")
    for u in REL_DOWNLOAD_RE.findall(text):
        yield u.rstrip(");,")
    for u in REL_DETAIL_RE.findall(text):
        yield u.rstrip(");,")
    for u in REL_REPORT_RE.findall(text):
        yield u.rstrip(");,")


def _classify_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    parts = urlsplit(url)
    path = parts.path or "/"
    low_path = path.lower()
    query = parts.query or ""
    hint = (hint_text or "").lower()
    if VIEW_FILE_PATH_RE.search(path):
        return "judgment"
    if DOWNLOAD_FILE_PATH_RE.search(path) and "doc=" in query.lower():
        return "judgment"
    if DETAIL_LISTING_PATH_RE.search(path) or LISTING_PATH_RE.search(path):
        return "listing"
    if low_path.endswith(".pdf") and DOC_HINT_RE.search(hint):
        return "judgment"
    return None


def _looks_like_report_grid(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower()
    if not path.endswith("/caselaw/public/rpt-afr"):
        return False
    low = (html_text or "").lower()
    return "reported-judgements-detail-all/" in low


def _looks_like_detail_grid(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower()
    if not DETAIL_LISTING_PATH_RE.search(path):
        return False
    low = (html_text or "").lower()
    return "download-file.php?doc=" in low and "<tbody" in low


def _view_file_from_download_url(download_url: str, *, base_url: str) -> Optional[str]:
    parts = urlsplit(download_url)
    if not DOWNLOAD_FILE_PATH_RE.search(parts.path or ""):
        return None
    values = parse_qs(parts.query or "", keep_blank_values=True).get("doc") or []
    if not values:
        return None
    token = html.unescape(values[0] or "").strip()
    if not token:
        return None
    token_path = quote(token, safe="-._~=%+")
    return normalize_shc_public_url(f"/caselaw/view-file/{token_path}", base_url=base_url)


def _file_view_token(url: str) -> Optional[str]:
    parts = urlsplit(url)
    if VIEW_FILE_PATH_RE.search(parts.path or ""):
        token = (parts.path or "").rstrip("/").split("/")[-1]
        return token or None
    if DOWNLOAD_FILE_PATH_RE.search(parts.path or ""):
        values = parse_qs(parts.query or "", keep_blank_values=True).get("doc") or []
        if values and values[0]:
            return values[0]
    return None


class SindhHighCourtPipeline(PublicPipeline):
    """Public pipeline with SHC result-grid and file-view discovery."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        docs: Dict[str, Dict[str, Any]] = {}
        listings: List[str] = []
        is_report_grid = _looks_like_report_grid(res.final_url, res.text)
        is_detail_grid = _looks_like_detail_grid(res.final_url, res.text)

        if is_detail_grid:
            # Detail grids can be very large; only row-window discovery is allowed for documents.
            self._collect_detail_grid_docs(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
            )
            self._collect_from_html(
                html_text=res.text,
                base_url=res.final_url,
                docs={},
                listings=listings,
            )
        else:
            self._collect_from_html(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
            )

        if is_report_grid:
            self._collect_report_grid_listings(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
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

    def _collect_report_grid_listings(self, *, html_text: str, base_url: str, listings: List[str]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_links: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            normalized = normalize_shc_public_url(href, base_url=base_url)
            if not normalized:
                continue
            try:
                safe = check_url_policy(
                    normalized,
                    self.source.allow_list or [],
                    document_cdn_hosts=self.source.document_cdn_hosts or [],
                    allow_private_for_tests=_tests_allow_private(),
                )
            except URLPolicyError:
                self.stats["rejected_urls"] += 1
                continue
            if DETAIL_LISTING_PATH_RE.search(urlsplit(safe).path or ""):
                detail_links.append(safe)

        page_size, max_pages = report_result_window_for(self.source)
        for _page, _page_index, nurl in _windowed(list(dict.fromkeys(detail_links)), page_size=page_size, max_pages=max_pages):
            listings.append(nurl)

    def _collect_detail_grid_docs(self, *, html_text: str, base_url: str, docs: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        rows = list(soup.select("tbody tr"))
        page_size, max_pages = detail_result_window_for(self.source)

        for page, page_index, row in _windowed(rows, page_size=page_size, max_pages=max_pages):
            row_meta = {
                "listing_fetch": "result_grid",
                "result_grid_kind": "reported-judgements-detail-all",
                "result_grid_endpoint": base_url,
                "result_window_page": page,
                "result_window_index": page_index,
                "result_window_page_size": page_size,
                "result_window_max_pages": max_pages,
            }
            for a in row.find_all("a", href=True):
                href = a.get("href", "")
                if "download-file.php?doc=" not in href.lower() and "view-file/" not in href.lower():
                    continue
                self._capture_candidate(
                    raw=href,
                    hint=f"row-anchor:{a.get_text(' ', strip=True)[:180]}",
                    channel="result-grid-row",
                    base_url=base_url,
                    docs=docs,
                    listings=[],
                    route_meta=row_meta,
                )
            for c in row.find_all(string=lambda node: isinstance(node, Comment)):
                for candidate in _extract_embedded_candidates(str(c)):
                    self._capture_candidate(
                        raw=candidate,
                        hint=f"row-comment:{str(candidate)[:180]}",
                        channel="result-grid-comment",
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
        normalized = normalize_shc_public_url(raw, base_url=base_url)
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
            judgment_candidates: List[Tuple[str, str, str]] = []
            derived_view = _view_file_from_download_url(safe, base_url=base_url)
            if derived_view:
                try:
                    derived_safe = check_url_policy(
                        derived_view,
                        self.source.allow_list or [],
                        document_cdn_hosts=self.source.document_cdn_hosts or [],
                        allow_private_for_tests=_tests_allow_private(),
                    )
                    judgment_candidates.append((derived_safe, "view-file", "download_doc_token"))
                except URLPolicyError:
                    self.stats["rejected_urls"] += 1

            if not judgment_candidates:
                endpoint_kind = "view-file" if VIEW_FILE_PATH_RE.search(urlsplit(safe).path or "") else "download-file"
                judgment_candidates.append((safe, endpoint_kind, "direct"))

            for candidate_url, pdf_endpoint_kind, pdf_source in judgment_candidates:
                meta = {
                    "discovery_channel": channel,
                    "discovery_hint": hint[:240],
                    "pdf_endpoint_kind": pdf_endpoint_kind,
                    "pdf_candidate_source": pdf_source,
                    "file_view_token": _file_view_token(candidate_url),
                    **route_meta,
                }
                existing = docs.get(candidate_url)
                if existing is None:
                    docs[candidate_url] = meta
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
                "result_grid_kind",
                "result_grid_endpoint",
                "result_window_page",
                "result_window_index",
                "result_window_page_size",
                "result_window_max_pages",
                "pdf_endpoint_kind",
                "pdf_candidate_source",
                "file_view_token",
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


async def scrape_sindh_high_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=SindhHighCourtPipeline,
        **kwargs,
    )
