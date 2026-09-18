"""
Azad Jammu & Kashmir High Court (PUBLIC judgments/orders) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - candidate links are normalized onto the AJK High Court public domain
  - judgment document links must pass the `%PDF` gate before ingestion

The public site exposes result rows after posting the search form on /important-judgments.
This connector performs a tiny, bounded set of allow-listed form posts and treats each
response as another listing page for link discovery.
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

PUBLIC_HOST = "ajkhighcourt.gok.pk"

DEFAULT_LISTINGS = [
    "https://ajkhighcourt.gok.pk/important-judgments",
    "https://ajkhighcourt.gok.pk/important-judgments?judgment_tab=previous",
]

DEFAULT_SEARCH_POSTS: List[Dict[str, str]] = [
    {"judgment_tab": "judgment", "cmbYear": "ALL", "cmbBench": "ALL", "cmbCategory": "ALL", "btnSearchJudgment": "Search"},
    {"judgment_tab": "previous", "cmbYear": "ALL", "cmbBench": "ALL", "cmbCategory": "ALL", "btnSearchJudgment": "Search"},
]
DEFAULT_SEARCH_RESULT_PAGE_SIZE = 200
DEFAULT_SEARCH_RESULT_MAX_PAGES = 2

DOC_HINT_RE = re.compile(r"(?i)(judg|order|appeal|petition|case|writ)")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?judgment_files/[^\"'<>]+)")
REL_LISTING_RE = re.compile(r"(?i)(/?important-judg(?:e)?ments(?:\?[^\"'<>\s]*)?)")


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("listings") or DEFAULT_LISTINGS
    return [{"url": u, "target_kind": "judgment"} for u in urls]


def normalize_ajk_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize a discovered candidate to AJK High Court public-domain URL form."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:")):
        return None
    candidate = _unwrap_wayback(candidate)
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    if re.match(r"(?i)^judgment_files/", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^important-judg(?:e)?ments(?:\?.*)?$", candidate):
        candidate = "/" + candidate
    if candidate.lower().startswith(("http://", "https://")):
        joined = candidate
    else:
        joined = urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    netloc = parts.netloc
    scheme = parts.scheme or "https"
    if host in ("ajkhighcourt.gok.pk", "www.ajkhighcourt.gok.pk"):
        scheme = "https"
        netloc = PUBLIC_HOST + (f":{parts.port}" if parts.port else "")
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
    path = (parts.path or "").lower()
    query = (parts.query or "").lower()
    hint = (hint_text or "").lower()
    is_pdf = path.endswith(".pdf") or ".pdf" in path
    in_doc_dir = "/judgment_files/" in path
    url_hint = DOC_HINT_RE.search(path)
    text_hint = DOC_HINT_RE.search(hint)
    if is_pdf and (in_doc_dir or url_hint or text_hint):
        return "judgment"
    if "important-judgments" in path or "important-judgements" in path:
        return "listing"
    if "judgment_tab=" in query and "important-judg" in path:
        return "listing"
    return None


def _looks_like_search_listing(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower().rstrip("/")
    if path != "/important-judgments":
        return False
    low = (html_text or "").lower()
    return "id=\"fmcs\"" in low and "name=\"cmbyear\"" in low and "name=\"cmbbench\"" in low and "name=\"cmbcategory\"" in low


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


def search_result_window_for(source: ScraperSource) -> Tuple[int, int]:
    cfg = source.config_json or {}
    page_size = _positive_int(cfg.get("search_result_page_size"), default=DEFAULT_SEARCH_RESULT_PAGE_SIZE)
    max_pages = _positive_int(cfg.get("search_result_max_pages"), default=DEFAULT_SEARCH_RESULT_MAX_PAGES)
    return (max(1, page_size), max(1, max_pages))


def _windowed(items: Sequence[Any], *, page_size: int, max_pages: int) -> Iterator[Tuple[int, int, Any]]:
    limit = page_size * max_pages
    for idx, item in enumerate(islice(items, limit)):
        yield ((idx // page_size) + 1, idx % page_size, item)


def _clean_text(text: str, *, limit: int = 260) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _search_results_rows(html_text: str) -> List[Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for table in soup.find_all("table"):
        classes = " ".join(table.get("class", [])).lower()
        header_text = _clean_text(table.get_text(" ", strip=True), limit=800).lower()
        if "court-table" in classes and "s.no" in header_text and "case title" in header_text and "judgment" in header_text:
            return [row for row in table.find_all("tr") if row.find("a", href=True)]
    return []


def search_posts_for(source: ScraperSource) -> List[Dict[str, str]]:
    cfg = source.config_json or {}
    raw = cfg.get("search_posts")
    if isinstance(raw, list):
        out: List[Dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            post = {str(k): str(v) for k, v in item.items() if v is not None}
            if not post:
                continue
            post.setdefault("cmbYear", "ALL")
            post.setdefault("cmbBench", "ALL")
            post.setdefault("cmbCategory", "ALL")
            post.setdefault("btnSearchJudgment", "Search")
            post.setdefault("judgment_tab", "judgment")
            out.append(post)
        if out:
            return out
    return [dict(p) for p in DEFAULT_SEARCH_POSTS]


class AJKHighCourtPipeline(PublicPipeline):
    """Public pipeline with AJK High Court listing + search-result discovery."""

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

        if _looks_like_search_listing(res.final_url, res.text):
            for payload in search_posts_for(self.source):
                safe_payload = {k: payload[k] for k in ("judgment_tab", "cmbYear", "cmbBench", "cmbCategory") if k in payload}
                try:
                    posted = await self.post_form(res.final_url, data=payload)
                except URLPolicyError:
                    continue
                route_meta = {"listing_fetch": "post_form", "search": safe_payload}
                found_result_rows = self._collect_post_result_table_docs(
                    html_text=posted.text,
                    base_url=posted.final_url,
                    docs=docs,
                    listings=listings,
                    route_meta=route_meta,
                )
                self._collect_from_html(
                    html_text=posted.text,
                    base_url=posted.final_url,
                    docs=docs,
                    listings=listings,
                    route_meta=route_meta,
                    allow_judgment_capture=not found_result_rows,
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

    def _collect_post_result_table_docs(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        route_meta: Dict[str, Any],
    ) -> bool:
        rows = _search_results_rows(html_text)
        if not rows:
            return False
        page_size, max_pages = search_result_window_for(self.source)
        for page, page_index, row in _windowed(rows, page_size=page_size, max_pages=max_pages):
            cells = row.find_all("td")
            serial = _clean_text(cells[0].get_text(" ", strip=True), limit=40) if len(cells) >= 1 else ""
            case_no = _clean_text((row.select_one(".court-case-no") or cells[1]).get_text(" ", strip=True), limit=220) if len(cells) >= 2 else ""
            title = _clean_text((row.select_one(".court-case-title") or cells[1]).get_text(" ", strip=True), limit=260) if len(cells) >= 2 else ""
            category = _clean_text(cells[2].get_text(" ", strip=True), limit=80) if len(cells) >= 3 else ""
            decision_date = _clean_text(cells[3].get_text(" ", strip=True), limit=40) if len(cells) >= 4 else ""
            row_meta: Dict[str, Any] = {
                **route_meta,
                "result_window_page": page,
                "result_window_index": page_index,
                "result_window_page_size": page_size,
                "result_window_max_pages": max_pages,
            }
            if serial:
                row_meta["result_row_serial"] = serial
            if case_no:
                row_meta["result_case_no"] = case_no
            if title:
                row_meta["result_title"] = title
            if category:
                row_meta["result_category"] = category
            if decision_date:
                row_meta["result_decision_date"] = decision_date
            for anchor in row.find_all("a", href=True):
                self._capture_candidate(
                    raw=anchor.get("href", ""),
                    hint=f"result-row:{case_no or title or serial}",
                    channel="result-table-row",
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
        normalized = normalize_ajk_public_url(raw, base_url=base_url)
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
                "search",
                "result_window_page",
                "result_window_index",
                "result_window_page_size",
                "result_window_max_pages",
                "result_row_serial",
                "result_case_no",
                "result_title",
                "result_category",
                "result_decision_date",
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


async def scrape_ajk_high_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=AJKHighCourtPipeline,
        **kwargs,
    )
