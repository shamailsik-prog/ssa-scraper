"""
Balochistan High Court (PUBLIC judgments) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - BHC result boxes are harvested for source-specific metadata
  - public BHC portal API rows are fanned out through bounded judge/year windows
  - candidate links are normalized onto public BHC hosts
  - judgment document links must pass the `%PDF` gate before ingestion
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from itertools import islice
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

PUBLIC_HOST = "bhc.gov.pk"
PORTAL_HOST = "portal.bhc.gov.pk"
API_HOST = "api.bhc.gov.pk"
PUBLIC_HOST_ALIASES = (
    PUBLIC_HOST,
    "www.bhc.gov.pk",
)

DEFAULT_LISTINGS = [
    "https://bhc.gov.pk/resources/judgments",
    "https://bhc.gov.pk/judgments",
    f"https://{PORTAL_HOST}/judgments",
]
DEFAULT_PORTAL_LOGIN_ENDPOINT = f"https://{API_HOST}/login"
DEFAULT_PORTAL_JUDGES_ENDPOINT = f"https://{API_HOST}/v2/judges"
DEFAULT_PORTAL_JUDGMENTS_ENDPOINT = f"https://{API_HOST}/v2/judgments"
DEFAULT_PORTAL_YEAR_START = 2001
DEFAULT_PORTAL_YEARS_BACK = 1
DEFAULT_PORTAL_JUDGE_MAX = 3
DEFAULT_PORTAL_RESULT_PAGE_SIZE = 200
DEFAULT_PORTAL_RESULT_MAX_PAGES = 2

DOC_HINT_RE = re.compile(r"(?i)(judg|judgement|judgment|order|case|appeal|petition|pld|mld|ylr|vs\b|v\.)")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?media/judgments/[^\"'<>\s]+?\.pdf(?:\?[^\"'<>\s]*)?)")
REL_LISTING_RE = re.compile(r"(?i)(/?(?:ur/)?resources/judgments(?:/[^\"'<>\s]*)?)")
REL_PORTAL_API_RE = re.compile(r"(?i)(/?v2/(?:judges|judgments|downloadpdf/[^\"'<>\s]+))")
PORTAL_BUNDLE_RE = re.compile(r"(?i)(?:href|src)=\"([^\"]*?/_nuxt/[A-Za-z0-9]+\.js)\"")
PORTAL_GUEST_RE = re.compile(
    r"""guestAuthData\s*:\s*\{\s*email\s*:\s*["']([^"']+)["']\s*,\s*password\s*:\s*["']([^"']+)["']""",
    re.I,
)

LISTING_PATH_RE = re.compile(r"(?i)^/(?:ur/)?resources/judgments(?:/.*)?/?$")
ROOT_LISTING_PATH_RE = re.compile(r"(?i)^/(?:ur/)?judgments/?$")
DOC_PATH_RE = re.compile(r"(?i)^/media/judgments/.+\.pdf$")
API_DOWNLOAD_PATH_RE = re.compile(r"(?i)^/v2/downloadpdf/[^/]+/[^/]+\.[a-z0-9]+$")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


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
    if re.match(r"(?i)^/?(?:ur/)?judgments(?:/?$|\?)", candidate):
        candidate = candidate if candidate.startswith("/") else "/" + candidate

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
    for u in REL_PORTAL_API_RE.findall(text):
        yield u.rstrip(");,")


def _is_judgment_pdf(url: str, *, hint_text: str) -> bool:
    parts = urlsplit(url)
    path = parts.path or "/"
    if API_DOWNLOAD_PATH_RE.search(path):
        return True
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


def _looks_like_portal_listing(url: str, html_text: str) -> bool:
    parts = urlsplit(url)
    path = (parts.path or "").lower()
    host = (parts.hostname or "").lower()
    if not ROOT_LISTING_PATH_RE.search(path):
        return False
    if host == PORTAL_HOST:
        return True
    low = (html_text or "").lower()
    return "__nuxt" in low and "/_nuxt/" in low


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


def _portal_years_for(source: ScraperSource) -> List[int]:
    cfg = source.config_json or {}
    raw_years = cfg.get("portal_years")
    if isinstance(raw_years, list):
        years: List[int] = []
        for item in raw_years:
            try:
                year = int(item)
            except Exception:
                continue
            if 1900 <= year <= 2100:
                years.append(year)
        if years:
            return sorted(set(years), reverse=True)
    now_year = datetime.now(UTC).year
    year_start = _positive_int(cfg.get("portal_year_start"), default=DEFAULT_PORTAL_YEAR_START)
    years_back = cfg.get("portal_years_back")
    try:
        lookback = max(0, int(years_back))
    except Exception:
        lookback = DEFAULT_PORTAL_YEARS_BACK
    floor = max(year_start, now_year - lookback)
    return list(range(now_year, floor - 1, -1))


def _portal_result_window_for(source: ScraperSource) -> Tuple[int, int]:
    cfg = source.config_json or {}
    page_size = _positive_int(cfg.get("portal_result_page_size"), default=DEFAULT_PORTAL_RESULT_PAGE_SIZE)
    max_pages = _positive_int(cfg.get("portal_result_max_pages"), default=DEFAULT_PORTAL_RESULT_MAX_PAGES)
    return (max(1, page_size), max(1, max_pages))


def _portal_judge_limit_for(source: ScraperSource) -> int:
    cfg = source.config_json or {}
    return max(1, _positive_int(cfg.get("portal_judge_max"), default=DEFAULT_PORTAL_JUDGE_MAX))


def _records_from_portal_json(json_text: str) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(json_text or "[]")
    except Exception:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "rows", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _chunked(items: Sequence[Dict[str, Any]], size: int) -> Iterator[List[Dict[str, Any]]]:
    it = iter(items)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            return
        yield chunk


class BalochistanHighCourtPipeline(PublicPipeline):
    """Public pipeline with BHC result-box and direct-PDF discovery."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sent_payload_signatures: set[str] = set()
        self._seen_response_hashes: set[str] = set()

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
        if _looks_like_portal_listing(res.final_url, res.text):
            await self._collect_portal_api_docs(
                listing_url=res.final_url,
                listing_html=res.text,
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

    async def _collect_portal_api_docs(self, *, listing_url: str, listing_html: str, docs: Dict[str, Dict[str, Any]]) -> None:
        cfg = self.source.config_json or {}
        if cfg.get("portal_enabled", True) is False:
            return
        login_endpoint = normalize_bhc_public_url(str(cfg.get("portal_login_endpoint") or DEFAULT_PORTAL_LOGIN_ENDPOINT), base_url=listing_url)
        judges_endpoint = normalize_bhc_public_url(str(cfg.get("portal_judges_endpoint") or DEFAULT_PORTAL_JUDGES_ENDPOINT), base_url=listing_url)
        judgments_endpoint = normalize_bhc_public_url(str(cfg.get("portal_judgments_endpoint") or DEFAULT_PORTAL_JUDGMENTS_ENDPOINT), base_url=listing_url)
        if not login_endpoint or not judges_endpoint or not judgments_endpoint:
            return

        creds = await self._portal_guest_credentials(listing_url=listing_url, listing_html=listing_html)
        if not creds:
            return
        guest_email, guest_password = creds
        portal_origin = f"{urlsplit(listing_url).scheme or 'https'}://{urlsplit(listing_url).netloc}"
        base_headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": portal_origin,
            "Referer": portal_origin.rstrip("/") + "/judgments/",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            login_res = await self.post_json(
                login_endpoint,
                payload={"email": guest_email, "password": guest_password},
                headers={**base_headers, "Referer": portal_origin.rstrip("/") + "/"},
            )
        except URLPolicyError:
            return
        token = self._portal_access_token(login_res.text or "")
        if not token:
            return
        auth_headers = {**base_headers, "Authorization": f"Bearer {token}"}

        try:
            judges_res = await self.post_json(judges_endpoint, payload={}, headers=auth_headers)
        except URLPolicyError:
            return
        judges_rows = _records_from_portal_json(judges_res.text or "")
        for payload, payload_meta in self._portal_search_payloads(judges_rows):
            payload_sig = sha256(json.dumps({"endpoint": judgments_endpoint, "payload": payload}, sort_keys=True).encode("utf-8")).hexdigest()
            if payload_sig in self._sent_payload_signatures:
                continue
            self._sent_payload_signatures.add(payload_sig)
            try:
                judgments_res = await self.post_json(judgments_endpoint, payload=payload, headers=auth_headers)
            except URLPolicyError:
                continue
            response_hash = sha256(judgments_res.content).hexdigest()
            if response_hash in self._seen_response_hashes:
                continue
            self._seen_response_hashes.add(response_hash)
            self._collect_portal_judgment_rows(
                rows=_records_from_portal_json(judgments_res.text or ""),
                base_url=judgments_res.final_url or judgments_endpoint,
                docs=docs,
                route_meta={
                    "listing_fetch": "portal_post_json",
                    "search_endpoint": judgments_endpoint,
                    "search": payload,
                    **payload_meta,
                },
            )

    async def _portal_guest_credentials(self, *, listing_url: str, listing_html: str) -> Optional[Tuple[str, str]]:
        cfg = self.source.config_json or {}
        configured_email = str(cfg.get("portal_guest_email") or "").strip()
        configured_password = str(cfg.get("portal_guest_password") or "").strip()
        if configured_email and configured_password:
            return (configured_email, configured_password)

        bundle_urls: List[str] = []
        for raw in PORTAL_BUNDLE_RE.findall(listing_html or "")[:8]:
            normalized = normalize_bhc_public_url(raw, base_url=listing_url)
            if normalized:
                bundle_urls.append(normalized)
        for bundle_url in list(dict.fromkeys(bundle_urls)):
            try:
                bundle_res = await self.fetch(bundle_url)
            except URLPolicyError:
                continue
            match = PORTAL_GUEST_RE.search(bundle_res.text or "")
            if match:
                return (match.group(1), match.group(2))
        return None

    def _portal_search_payloads(self, judges_rows: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        cfg = self.source.config_json or {}
        raw_posts = cfg.get("portal_search_posts")
        if isinstance(raw_posts, list):
            planned: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            for post in raw_posts:
                if not isinstance(post, dict):
                    continue
                payload = {str(k): v for k, v in post.items() if v not in ("", None)}
                if payload:
                    planned.append((payload, {"search_source": "portal_search_posts"}))
            if planned:
                return planned

        candidates: List[Tuple[int, int, int, str]] = []
        for row in judges_rows:
            if not isinstance(row, dict):
                continue
            try:
                judge_id = int(row.get("JUDGE_ID"))
            except Exception:
                continue
            try:
                status = int(row.get("STATUS") or 0)
            except Exception:
                status = 0
            try:
                orders = int(row.get("TOTAL_ORDERS") or 0)
            except Exception:
                orders = 0
            judge_name = str(row.get("JUDGE_NAME") or "").strip()
            candidates.append((status, orders, judge_id, judge_name))

        selected = sorted(candidates, key=lambda item: (item[0], item[1], item[2]), reverse=True)[: _portal_judge_limit_for(self.source)]
        years = _portal_years_for(self.source)
        payloads: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for status, _orders, judge_id, judge_name in selected:
            for year in years:
                payloads.append(
                    (
                        {
                            "searchBy": 3,
                            "judgeId": judge_id,
                            "caseYear": year,
                            "sDate": f"{year}-01-01",
                            "eDate": f"{year}-12-31",
                        },
                        {
                            "search_source": "portal_judge_year",
                            "portal_judge_id": judge_id,
                            "portal_judge_status": status,
                            "portal_judge_name": judge_name[:180],
                        },
                    )
                )
        return payloads

    def _collect_portal_judgment_rows(
        self,
        *,
        rows: List[Dict[str, Any]],
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        if not rows:
            return
        page_size, max_pages = _portal_result_window_for(self.source)
        for page_idx, chunk in enumerate(_chunked(rows, page_size), start=1):
            if page_idx > max_pages:
                break
            page_meta = {
                **route_meta,
                "search_result_page": page_idx,
                "search_result_page_size": page_size,
                "search_result_max_pages": max_pages,
            }
            for row_idx, row in enumerate(chunk):
                candidate = self._portal_download_url_from_row(row, base_url=base_url)
                if not candidate:
                    continue
                hint = f"portal-row:{str(row.get('REGISTER_NUMBER') or row.get('CASE_TITLE') or row.get('CASE_ID') or '')[:180]}"
                self._capture_candidate(
                    raw=candidate,
                    hint=hint,
                    channel="portal-api-row",
                    base_url=base_url,
                    docs=docs,
                    listings=[],
                    route_meta={
                        **page_meta,
                        "search_result_index": row_idx,
                        "portal_case_id": row.get("CASE_ID"),
                        "portal_case_number": (str(row.get("REGISTER_NUMBER") or "") or "")[:120],
                        "portal_order_date": (str(row.get("ORDER_DATE") or "") or "")[:40],
                        "portal_type_name": (str(row.get("TYPE_NAME") or "") or "")[:80],
                    },
                )

    def _portal_download_url_from_row(self, row: Dict[str, Any], *, base_url: str) -> Optional[str]:
        folder = str(row.get("FILE_FOLDER") or "").strip().strip("/")
        name = str(row.get("FILE_NAME") or "").strip().strip("/")
        ext = str(row.get("FILE_EXT") or "").strip().lower()
        if not folder or not name:
            return None
        if not ext:
            ext = "pdf"
        if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
            return None
        safe_folder = quote(folder, safe="")
        safe_name = quote(name, safe="")
        return normalize_bhc_public_url(f"/v2/downloadpdf/{safe_folder}/{safe_name}.{ext}", base_url=base_url)

    def _portal_access_token(self, json_text: str) -> Optional[str]:
        try:
            payload = json.loads(json_text or "{}")
        except Exception:
            return None
        token = str((payload or {}).get("access_token") or "").strip()
        return token or None

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
            path = urlsplit(safe).path or "/"
            pdf_endpoint_kind = "portal-downloadpdf" if API_DOWNLOAD_PATH_RE.search(path) else "media-judgments"
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
                "search_endpoint",
                "search",
                "search_source",
                "search_result_page",
                "search_result_index",
                "search_result_page_size",
                "search_result_max_pages",
                "portal_judge_id",
                "portal_judge_status",
                "portal_judge_name",
                "portal_case_id",
                "portal_case_number",
                "portal_order_date",
                "portal_type_name",
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
