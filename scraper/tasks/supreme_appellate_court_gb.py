"""
Supreme Appellate Court Gilgit-Baltistan (PUBLIC judgments/orders) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - candidate links are normalized onto the SAC-GB public domain
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

PUBLIC_HOST = "sacgb.gov.pk"
PUBLIC_HOST_ALIASES = (PUBLIC_HOST, f"www.{PUBLIC_HOST}")

DEFAULT_LISTINGS = [
    "https://sacgb.gov.pk/Judgments.html",
    "https://sacgb.gov.pk/Latest%20Judgements.html",
]
DEFAULT_RESULT_PAGE_SIZE = 200
DEFAULT_RESULT_MAX_PAGES = 2

DOC_HINT_RE = re.compile(r"(?i)(judg(?:e)?ment|order|appeal|petition|case|vs\.?|v\.?\s)")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?Judgments?/[^\"'<>]+?\.pdf(?:\?[^\"'<>]*)?)")
REL_LISTING_RE = re.compile(r"(?i)(/?(?:Judgments\.html|Latest(?:%20|\s)+Judgements\.html)(?:\?[^\"'<>\s]*)?)")
LISTING_PATH_RE = re.compile(r"(?i)^/(judgments\.html|latest(?:%20|\s)+judgements\.html)$")
JUDGMENTS_LISTING_PATH_RE = re.compile(r"(?i)^/judgments\.html$")
LATEST_JUDGEMENTS_LISTING_PATH_RE = re.compile(r"(?i)^/latest(?:%20|\s)+judgements\.html$")


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


def normalize_sacgb_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize a discovered candidate to SAC-GB public-domain URL form."""
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
    if re.match(r"(?i)^judgments?/", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^(judgments\.html|latest(?:%20|\s)+judgements\.html)(?:\?.*)?$", candidate):
        candidate = "/" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    netloc = parts.netloc
    scheme = parts.scheme or "https"
    if host in PUBLIC_HOST_ALIASES:
        scheme = "https"
        netloc = PUBLIC_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
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
    path = parts.path or "/"
    low_path = path.lower()
    hint = (hint_text or "").lower()

    in_judgments_dir = "/judgments/" in low_path or "/judgements/" in low_path
    is_pdf = low_path.endswith(".pdf") or ".pdf" in low_path
    if is_pdf and (in_judgments_dir or DOC_HINT_RE.search(low_path) or DOC_HINT_RE.search(hint)):
        return "judgment"
    if LISTING_PATH_RE.search(path):
        return "listing"
    return None


def _judgments_table_rows(html_text: str) -> List[Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    table = soup.find("table", id=lambda v: isinstance(v, str) and v.lower() == "mytable")
    if table is None:
        return []
    return [row for row in table.find_all("tr") if row.find("a", href=True)]


def _latest_judgements_table_rows(html_text: str) -> List[Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for table in soup.find_all("table"):
        header = _clean_text(table.get_text(" ", strip=True), limit=1000).lower()
        if "sr.no" in header and "case subject" in header and "case no." in header and "download" in header:
            return [row for row in table.find_all("tr") if row.find("a", href=True)]
    return []


def _looks_like_judgments_table_listing(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "/")
    if not JUDGMENTS_LISTING_PATH_RE.search(path):
        return False
    return bool(_judgments_table_rows(html_text))


def _looks_like_latest_judgements_listing(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "/")
    if not LATEST_JUDGEMENTS_LISTING_PATH_RE.search(path):
        return False
    return bool(_latest_judgements_table_rows(html_text))


class SupremeAppellateCourtGBPipeline(PublicPipeline):
    """Public pipeline with SAC-GB robust listing discovery."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        docs: Dict[str, Dict[str, Any]] = {}
        listings: List[str] = []
        is_judgments_table = _looks_like_judgments_table_listing(res.final_url, res.text)
        is_latest_table = _looks_like_latest_judgements_listing(res.final_url, res.text)

        for raw, channel, hint in _iter_discovery_candidates(res.text):
            self._capture_candidate(
                raw=raw,
                hint=hint,
                channel=channel,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
                route_meta={},
                allow_judgment_capture=not (is_judgments_table or is_latest_table),
            )

        if is_judgments_table:
            self._collect_judgments_table_docs(html_text=res.text, base_url=res.final_url, docs=docs, listings=listings)
        if is_latest_table:
            self._collect_latest_judgements_docs(html_text=res.text, base_url=res.final_url, docs=docs, listings=listings)

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

    def _collect_judgments_table_docs(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
    ) -> None:
        rows = _judgments_table_rows(html_text)
        if not rows:
            return
        page_size, max_pages = result_window_for(self.source)
        for page, page_index, row in _windowed(rows, page_size=page_size, max_pages=max_pages):
            row_text = _clean_text(row.get_text(" ", strip=True), limit=280)
            row_meta: Dict[str, Any] = {
                "listing_fetch": "result_table",
                "result_table_kind": "judgments",
                "result_window_page": page,
                "result_window_index": page_index,
                "result_window_page_size": page_size,
                "result_window_max_pages": max_pages,
            }
            if row_text:
                row_meta["result_title"] = row_text
            for anchor in row.find_all("a", href=True):
                self._capture_candidate(
                    raw=anchor.get("href", ""),
                    hint=f"result-row:{row_text or page_index + 1}",
                    channel="result-table-row",
                    base_url=base_url,
                    docs=docs,
                    listings=listings,
                    route_meta=row_meta,
                )

    def _collect_latest_judgements_docs(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
    ) -> None:
        rows = _latest_judgements_table_rows(html_text)
        if not rows:
            return
        page_size, max_pages = result_window_for(self.source)
        for page, page_index, row in _windowed(rows, page_size=page_size, max_pages=max_pages):
            cells = row.find_all("td")
            serial = _clean_text(cells[0].get_text(" ", strip=True), limit=40) if len(cells) >= 1 else ""
            case_subject = _clean_text(cells[1].get_text(" ", strip=True), limit=220) if len(cells) >= 2 else ""
            case_no = _clean_text(cells[2].get_text(" ", strip=True), limit=220) if len(cells) >= 3 else ""
            title = _clean_text(cells[3].get_text(" ", strip=True), limit=260) if len(cells) >= 4 else ""
            author_judge = _clean_text(cells[4].get_text(" ", strip=True), limit=220) if len(cells) >= 5 else ""
            judgment_date = _clean_text(cells[5].get_text(" ", strip=True), limit=60) if len(cells) >= 6 else ""
            upload_date = _clean_text(cells[6].get_text(" ", strip=True), limit=60) if len(cells) >= 7 else ""
            row_meta: Dict[str, Any] = {
                "listing_fetch": "result_table",
                "result_table_kind": "latest_judgements",
                "result_window_page": page,
                "result_window_index": page_index,
                "result_window_page_size": page_size,
                "result_window_max_pages": max_pages,
            }
            if serial:
                row_meta["result_row_serial"] = serial
            if case_subject:
                row_meta["result_case_subject"] = case_subject
            if case_no:
                row_meta["result_case_no"] = case_no
            if title:
                row_meta["result_title"] = title
            if author_judge:
                row_meta["result_author_judge"] = author_judge
            if judgment_date:
                row_meta["result_judgment_date"] = judgment_date
            if upload_date:
                row_meta["result_upload_date"] = upload_date
            for anchor in row.find_all("a", href=True):
                self._capture_candidate(
                    raw=anchor.get("href", ""),
                    hint=f"latest-row:{case_no or title or serial}",
                    channel="result-table-row",
                    base_url=base_url,
                    docs=docs,
                    listings=listings,
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
        normalized = normalize_sacgb_public_url(raw, base_url=base_url)
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
                "result_table_kind",
                "result_window_page",
                "result_window_index",
                "result_window_page_size",
                "result_window_max_pages",
                "result_row_serial",
                "result_case_subject",
                "result_case_no",
                "result_title",
                "result_author_judge",
                "result_judgment_date",
                "result_upload_date",
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


async def scrape_supreme_appellate_court_gb(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=SupremeAppellateCourtGBPipeline,
        **kwargs,
    )
