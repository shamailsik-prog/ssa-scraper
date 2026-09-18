"""
Federal Shariat Court (PUBLIC judgments/orders) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - candidate links are normalised onto the FSC public domain
  - judgment/order document links must pass the `%PDF` gate before ingestion
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

PUBLIC_HOST = "www.federalshariatcourt.gov.pk"

DEFAULT_LISTINGS = [
    "https://www.federalshariatcourt.gov.pk/en/judgments/",
    "https://www.federalshariatcourt.gov.pk/judnew1.html",
    "https://www.federalshariatcourt.gov.pk/alljud.php",
    "https://www.federalshariatcourt.gov.pk/en/orders/",
]
DEFAULT_RESULT_PAGE_SIZE = 200
DEFAULT_RESULT_MAX_PAGES = 2

DOC_HINT_RE = re.compile(r"(?i)(judg|order|appeal|petition|case)")
JUDGMENT_DIR_RE = re.compile(r"(?i)^/(judgments|judgements|judments)/")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?(?:judgments|judgements|judments)/[^\"'<>]+)")
REL_LISTING_RE = re.compile(
    r"(?i)((?:/?(?:alljud\.php(?:\?[^\"'<>\s]*)?|judnew\d*\.html|en/(?:judgments|orders|leading-judgements)(?:/page/\d+)?/?(?:\?(?:paged|page)=\d+)?)))"
)
LISTING_PATH_RE = re.compile(r"(?i)(/alljud\.php|/judnew\d*\.html|/en/judgments/?|/en/judgments/page/\d+/?|/en/orders/?|/en/orders/page/\d+/?|/en/leading-judgements/?|/en/leading-judgements/page/\d+/?)")
ORDERS_UPLOAD_RE = re.compile(r"(?i)^/wp-content/uploads/.+/orders/.+\.pdf$")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


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


def _clean_text(text: str, *, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _first_year(*fragments: str) -> Optional[str]:
    for fragment in fragments:
        m = YEAR_RE.search(fragment or "")
        if m:
            return m.group(0)
    return None


def normalize_fsc_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize a discovered candidate to FSC public-domain URL form."""
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
    if re.match(r"(?i)^(judgments|judgements|judments)/", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^(alljud\.php(?:\?.*)?|judnew\d*\.html)$", candidate):
        candidate = "/" + candidate
    if candidate.lower().startswith(("http://", "https://")):
        joined = candidate
    else:
        joined = urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    netloc = parts.netloc
    scheme = parts.scheme or "https"
    if host in ("federalshariatcourt.gov.pk", "www.federalshariatcourt.gov.pk"):
        scheme = "https"
        netloc = PUBLIC_HOST + (f":{parts.port}" if parts.port else "")
    path = parts.path or "/"
    path = _normalize_known_path_typos(path)
    path = quote(path, safe="/%:@,+;=()-.~_")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _unwrap_wayback(url: str) -> str:
    m = WAYBACK_RE.search(url)
    if m:
        return m.group(1)
    return url


def _normalize_known_path_typos(path: str) -> str:
    out = path if path.startswith("/") else "/" + path
    out = re.sub(r"(?i)^/en/(judgments|orders|leading-judgements)/(judgments|judgements|judments)/", "/Judgments/", out)
    out = re.sub(r"(?i)^/(judgements|judments)/", "/Judgments/", out)
    out = re.sub(r"(?i)^/judgments/", "/Judgments/", out)
    return out


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
    in_judgment_dir = bool(JUDGMENT_DIR_RE.search(path))
    is_pdf = path.endswith(".pdf") or ".pdf" in path
    url_hint = DOC_HINT_RE.search(path) or "order" in path
    text_hint = DOC_HINT_RE.search(hint or "")
    if is_pdf and (in_judgment_dir or url_hint or text_hint):
        return "judgment"
    if in_judgment_dir and not path.endswith((".html", ".php", "/")):
        return "judgment"
    has_wp_paged_query = bool(re.search(r"(^|&)(paged|page)=\d+($|&)", query))
    is_wp_listing = bool(re.search(r"^/en/(judgments|orders|leading-judgements)(/page/\d+)?/?$", path))
    if "alljud.php" in path or re.search(r"/judnew\d*\.html$", path) or LISTING_PATH_RE.search(path) or ("page=" in query and "alljud.php" in path):
        return "listing"
    if is_wp_listing and (not query or has_wp_paged_query):
        return "listing"
    return None


class FederalShariatCourtPipeline(PublicPipeline):
    """Public pipeline with FSC-specific robust listing discovery."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        docs: Dict[str, Dict[str, Any]] = {}
        listings: List[str] = []
        is_alljud_table = _looks_like_alljud_table(res.final_url, res.text)
        is_orders_table = _looks_like_orders_table(res.final_url, res.text)

        for raw, channel, hint in _iter_discovery_candidates(res.text):
            self._capture_candidate(
                raw=raw,
                hint=hint,
                channel=channel,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
                route_meta={},
                allow_judgment_capture=not (is_alljud_table or is_orders_table),
            )

        if is_alljud_table:
            self._collect_alljud_table_docs(html_text=res.text, base_url=res.final_url, docs=docs)
        if is_orders_table:
            self._collect_orders_table_docs(html_text=res.text, base_url=res.final_url, docs=docs)

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

    def _collect_alljud_table_docs(self, *, html_text: str, base_url: str, docs: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        table = _alljud_table(soup)
        if table is None:
            return
        rows = [row for row in table.find_all("tr") if row.find("a", href=True)]
        page_size, max_pages = result_window_for(self.source)
        for page, page_index, row in _windowed(rows, page_size=page_size, max_pages=max_pages):
            cells = row.find_all("td")
            serial = _clean_text(cells[0].get_text(" ", strip=True), limit=40) if len(cells) >= 1 else ""
            case_no = _clean_text(cells[1].get_text(" ", strip=True), limit=200) if len(cells) >= 2 else ""
            title = _clean_text(cells[2].get_text(" ", strip=True), limit=260) if len(cells) >= 3 else ""
            decision_year = _first_year(
                _clean_text(cells[4].get_text(" ", strip=True), limit=40) if len(cells) >= 5 else "",
                case_no,
                title,
            )
            row_meta: Dict[str, Any] = {
                "listing_fetch": "result_table",
                "result_table_kind": "alljud",
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
            if decision_year:
                row_meta["result_decision_year"] = decision_year
            for anchor in row.find_all("a", href=True):
                self._capture_candidate(
                    raw=anchor.get("href", ""),
                    hint=f"alljud-row:{case_no or title or serial}",
                    channel="result-table-row",
                    base_url=base_url,
                    docs=docs,
                    listings=[],
                    route_meta=row_meta,
                )

    def _collect_orders_table_docs(self, *, html_text: str, base_url: str, docs: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        table = _orders_table(soup)
        if table is None:
            return
        rows = [row for row in table.find_all("tr") if row.find("a", href=True)]
        page_size, max_pages = result_window_for(self.source)
        for page, page_index, row in _windowed(rows, page_size=page_size, max_pages=max_pages):
            cells = row.find_all("td")
            serial = _clean_text(cells[0].get_text(" ", strip=True), limit=40) if len(cells) >= 1 else ""
            order_date = _clean_text(cells[1].get_text(" ", strip=True), limit=40) if len(cells) >= 2 else ""
            title = _clean_text(cells[2].get_text(" ", strip=True), limit=260) if len(cells) >= 3 else ""
            row_meta: Dict[str, Any] = {
                "listing_fetch": "result_table",
                "result_table_kind": "orders",
                "result_window_page": page,
                "result_window_index": page_index,
                "result_window_page_size": page_size,
                "result_window_max_pages": max_pages,
            }
            if serial:
                row_meta["result_row_serial"] = serial
            if order_date:
                row_meta["result_order_date"] = order_date
            if title:
                row_meta["result_title"] = title
            for anchor in row.find_all("a", href=True):
                self._capture_candidate(
                    raw=anchor.get("href", ""),
                    hint=f"orders-row:{title or order_date or serial}",
                    channel="result-table-row",
                    base_url=base_url,
                    docs=docs,
                    listings=[],
                    route_meta=row_meta,
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
        allow_judgment_capture: bool = True,
    ) -> None:
        normalized = normalize_fsc_public_url(raw, base_url=base_url)
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
            path = urlsplit(safe).path or "/"
            if JUDGMENT_DIR_RE.search(path):
                pdf_endpoint_kind = "judgments-dir"
            elif ORDERS_UPLOAD_RE.search(path):
                pdf_endpoint_kind = "orders-upload"
            else:
                pdf_endpoint_kind = "direct-pdf"
            meta = {
                "discovery_channel": channel,
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": pdf_endpoint_kind,
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
                "result_table_kind",
                "result_window_page",
                "result_window_index",
                "result_window_page_size",
                "result_window_max_pages",
                "result_row_serial",
                "result_case_no",
                "result_title",
                "result_decision_year",
                "result_order_date",
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


def _alljud_table(soup: BeautifulSoup) -> Optional[Any]:
    for table in soup.find_all("table"):
        header = _clean_text(table.get_text(" ", strip=True), limit=800).lower()
        if "year of decision" in header and "case no" in header:
            return table
    return None


def _orders_table(soup: BeautifulSoup) -> Optional[Any]:
    for table in soup.find_all("table"):
        header = _clean_text(table.get_text(" ", strip=True), limit=800).lower()
        if "s.no" in header and "orders" in header and "date" in header:
            return table
    return None


def _looks_like_alljud_table(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower()
    if not path.endswith("/alljud.php"):
        return False
    low = (html_text or "").lower()
    return "year of decision" in low and "case no" in low and "judgments/" in low


def _looks_like_orders_table(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower()
    if not path.endswith("/en/orders/"):
        return False
    low = (html_text or "").lower()
    return "<table" in low and "s.no" in low and "orders" in low and "/orders/" in low


async def scrape_federal_shariat_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=FederalShariatCourtPipeline,
        **kwargs,
    )
