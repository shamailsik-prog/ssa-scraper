"""
Islamabad High Court (PUBLIC judgments) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - IHC AJAX JSON responses are harvested through bounded POST payloads
  - candidate links are normalized onto public IHC domains
  - judgment document links must pass the `%PDF` gate before ingestion
"""

from __future__ import annotations

import html
import json
import re
from hashlib import sha256
from itertools import islice
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

PUBLIC_HOST = "mis.ihc.gov.pk"
PUBLIC_HOST_ALIASES = (PUBLIC_HOST, "ihc.gov.pk", "www.ihc.gov.pk")

DEFAULT_LISTINGS = [
    "https://mis.ihc.gov.pk/frmJgmnt.aspx?jgs=1",
    "https://mis.ihc.gov.pk/frmJgmnt.aspx?jgs=0",
]
DEFAULT_LATEST_ENDPOINT = "/ihc.asmx/GetLatestJgmntsNew"
DEFAULT_SEARCH_ENDPOINT = "/ihc.asmx/srchDecisionClms"
DEFAULT_SEARCH_POSTS: List[Dict[str, str]] = [
    {
        "PCASENO": "0",
        "PJUG": "0",
        "PADV": "0",
        "PYEAR": "0",
        "pPrty": "",
        "PDDATE": "01/01/1900",
        "PLANDMARK": "1",
        "PAFR": "0",
    }
]
DEFAULT_SEARCH_RESULT_PAGE_SIZE = 200
DEFAULT_SEARCH_RESULT_MAX_PAGES = 2

DOC_HINT_RE = re.compile(r"(?i)(judg|jgmnt|order|appeal|petition|writ|case)")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?attachments/judgements/[^\"'<>]+?\.pdf(?:\?[^\"'<>]*)?)")
REL_LISTING_RE = re.compile(r"(?i)(/?frm(?:Jgmnt|RdJgmnt)(?:\.aspx)?(?:\?[^\"'<>]*)?)")
REL_AJAX_RE = re.compile(r"(?i)(/?ihc\.asmx/(?:GetLatestJgmntsNew|srchDecisionClms)(?:\?[^\"'<>]*)?)")
LISTING_PATH_RE = re.compile(r"(?i)^/frmjgmnt(?:\.aspx)?/?$")
DETAIL_PATH_RE = re.compile(r"(?i)^/frmrdjgmnt(?:\.aspx)?/?$")
AJAX_PATH_RE = re.compile(r"(?i)^/ihc\.asmx/(getlatestjgmntsnew|srchdecisionclms)$")
AJAX_ENDPOINT_RE = re.compile(r"""(?i)\bwebMethod\s*=\s*["']([^"']*ihc\.asmx/[^"']+)["']""")

JSON_FILE_KEYS = frozenset(
    {
        "attachments",
        "attachment",
        "jgmnt",
        "download",
        "downloadurl",
        "download_url",
        "pdf",
        "pdfurl",
        "pdf_url",
        "url",
        "href",
        "link",
    }
)


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("listings") or DEFAULT_LISTINGS
    return [{"url": u, "target_kind": "judgment"} for u in urls]


def normalize_ihc_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize a discovered candidate to IHC public-domain URL form."""
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
    if re.match(r"(?i)^attachments/judgements/", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^frm(?:jgmnt|rdjgmnt)(?:\\.aspx)?(?:\\?.*)?$", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^ihc\\.asmx/(?:GetLatestJgmntsNew|srchDecisionClms)(?:\\?.*)?$", candidate):
        candidate = "/" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PUBLIC_HOST_ALIASES:
        scheme = "https"
        canonical_host = PUBLIC_HOST if host == PUBLIC_HOST else host
        netloc = canonical_host + (f":{parts.port}" if parts.port else "")
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
    for u in REL_AJAX_RE.findall(text):
        yield u.rstrip(");,")


def _classify_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    parts = urlsplit(url)
    path = parts.path or "/"
    low_path = path.lower()
    hint = (hint_text or "").lower()
    is_pdf = low_path.endswith(".pdf") or ".pdf" in low_path
    in_doc_dir = "/attachments/judgements/" in low_path
    if is_pdf and (in_doc_dir or DOC_HINT_RE.search(low_path) or DOC_HINT_RE.search(hint)):
        return "judgment"
    if AJAX_PATH_RE.search(path):
        return None
    if LISTING_PATH_RE.search(path) or DETAIL_PATH_RE.search(path):
        return "listing"
    return None


def _looks_like_ihc_listing(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower()
    if not LISTING_PATH_RE.search(path):
        return False
    low = (html_text or "").lower()
    return "getlatestjgmntsnew" in low or "srchdecisionclms" in low or "ihc.asmx/" in low


def latest_endpoint_for(source: ScraperSource, *, base_url: str, html_text: str) -> Optional[str]:
    cfg = source.config_json or {}
    endpoint = cfg.get("latest_endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        return normalize_ihc_public_url(endpoint, base_url=base_url)
    if "GetLatestJgmntsNew" in (html_text or ""):
        return normalize_ihc_public_url(DEFAULT_LATEST_ENDPOINT, base_url=base_url)
    return None


def search_endpoint_for(source: ScraperSource, *, base_url: str, html_text: str) -> Optional[str]:
    cfg = source.config_json or {}
    endpoint = cfg.get("search_endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        return normalize_ihc_public_url(endpoint, base_url=base_url)
    for match in AJAX_ENDPOINT_RE.findall(html_text or ""):
        if "srchDecisionClms" in match:
            return normalize_ihc_public_url(match, base_url=base_url)
    if "srchDecisionClms" in (html_text or ""):
        return normalize_ihc_public_url(DEFAULT_SEARCH_ENDPOINT, base_url=base_url)
    return None


def latest_posts_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    raw = cfg.get("latest_posts")
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                out.append({str(k): v for k, v in item.items()})
        if out:
            return out
    return [{}]


def search_posts_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    raw = cfg.get("search_posts")
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            out.append({str(k): str(v) for k, v in item.items() if v is not None})
        if out:
            return out
    return [dict(p) for p in DEFAULT_SEARCH_POSTS]


def search_result_window_for(source: ScraperSource) -> Tuple[int, int]:
    cfg = source.config_json or {}
    page_size = _positive_int(cfg.get("search_result_page_size"), default=DEFAULT_SEARCH_RESULT_PAGE_SIZE)
    max_pages = _positive_int(cfg.get("search_result_max_pages"), default=DEFAULT_SEARCH_RESULT_MAX_PAGES)
    return (max(1, page_size), max(1, max_pages))


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


def _iter_json_candidates(payload: Any) -> Iterable[Tuple[str, str]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lkey = str(key).lower()
            if isinstance(value, str):
                if lkey in JSON_FILE_KEYS:
                    yield (value, str(key))
                for candidate in _extract_embedded_candidates(value):
                    yield (candidate, str(key))
            elif isinstance(value, (dict, list)):
                for candidate, path in _iter_json_candidates(value):
                    yield (candidate, f"{key}.{path}")
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            if isinstance(item, str):
                for candidate in _extract_embedded_candidates(item):
                    yield (candidate, str(idx))
            elif isinstance(item, (dict, list)):
                for candidate, path in _iter_json_candidates(item):
                    yield (candidate, f"[{idx}].{path}")


def _records_from_asmx_json(json_text: str) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(json_text or "{}")
    except Exception:
        return []
    body: Any = payload
    if isinstance(payload, dict) and "d" in payload:
        body = payload.get("d")
    if isinstance(body, str):
        stripped = body.strip()
        if stripped == '"empty"' or stripped == "empty":
            return []
        try:
            body = json.loads(stripped)
        except Exception:
            return []
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    return []


def _chunked(items: Sequence[Dict[str, Any]], size: int) -> Iterator[List[Dict[str, Any]]]:
    it = iter(items)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            return
        yield chunk


def _detail_url_from_row(row: Dict[str, Any], *, base_url: str) -> Optional[str]:
    case_no = str(row.get("CASENO") or "").strip()
    attachment = str(row.get("ATTACHMENTS") or "").strip()
    if not case_no or not attachment:
        return None
    citation = str(row.get("O_CITATION") or "").strip()
    cse_title = str(row.get("PARTIES") or "").strip()
    bench = str(row.get("BENCHNAME") or "").strip()
    query = urlencode(
        {
            "cseNo": f"{case_no} | {citation}".strip(),
            "cseTle": cse_title,
            "jgs": bench,
            "jgmnt": attachment,
        },
        doseq=False,
        quote_via=quote,
        safe="/:|",
    )
    return normalize_ihc_public_url(f"/frmRdJgmnt.aspx?{query}", base_url=base_url)


def _candidate_from_detail_query(url: str, *, base_url: str) -> Optional[str]:
    parts = urlsplit(url)
    if not DETAIL_PATH_RE.search(parts.path or ""):
        return None
    query = parse_qs(parts.query or "")
    values = query.get("jgmnt") or query.get("JGMNT") or []
    if not values:
        return None
    raw = html.unescape(values[0] or "").strip()
    if not raw:
        return None
    return normalize_ihc_public_url(raw, base_url=base_url)


class IslamabadHighCourtPipeline(PublicPipeline):
    """Public pipeline with IHC listing + AJAX JSON discovery."""

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

        if _looks_like_ihc_listing(res.final_url, res.text):
            latest_endpoint = latest_endpoint_for(self.source, base_url=res.final_url, html_text=res.text)
            if latest_endpoint:
                await self._harvest_json_posts(
                    endpoint_url=latest_endpoint,
                    payloads=latest_posts_for(self.source),
                    listing_url=res.final_url,
                    docs=docs,
                    listings=listings,
                    source_tag="latest",
                )
            search_endpoint = search_endpoint_for(self.source, base_url=res.final_url, html_text=res.text)
            if search_endpoint:
                await self._harvest_json_posts(
                    endpoint_url=search_endpoint,
                    payloads=search_posts_for(self.source),
                    listing_url=res.final_url,
                    docs=docs,
                    listings=listings,
                    source_tag="search",
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

    async def _harvest_json_posts(
        self,
        *,
        endpoint_url: str,
        payloads: List[Dict[str, Any]],
        listing_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        source_tag: str,
    ) -> None:
        try:
            safe_endpoint = check_url_policy(
                endpoint_url,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        page_size, max_pages = search_result_window_for(self.source)
        for payload in payloads:
            payload_sig = sha256(json.dumps({"endpoint": safe_endpoint, "payload": payload}, sort_keys=True).encode("utf-8")).hexdigest()
            if payload_sig in self._sent_payload_signatures:
                continue
            self._sent_payload_signatures.add(payload_sig)
            try:
                posted = await self.post_json(safe_endpoint, payload=payload)
            except URLPolicyError:
                continue
            response_hash = sha256(posted.content).hexdigest()
            if response_hash in self._seen_response_hashes:
                continue
            self._seen_response_hashes.add(response_hash)
            route_meta = {
                "listing_fetch": "post_json",
                "search_endpoint": safe_endpoint,
                "search": {k: v for k, v in payload.items() if v not in ("", None)},
                "search_source": source_tag,
            }
            self._collect_from_json(
                json_text=posted.text or "",
                base_url=posting_final_url(posted.final_url, safe_endpoint),
                docs=docs,
                listings=listings,
                route_meta=route_meta,
            )
            records = _records_from_asmx_json(posted.text or "")
            if not records:
                continue
            for idx, chunk in enumerate(_chunked(records, page_size), start=1):
                if idx > max_pages:
                    break
                page_meta = {**route_meta, "search_result_page": idx}
                self._collect_from_records(chunk, base_url=posted.final_url or safe_endpoint, docs=docs, listings=listings, route_meta=page_meta)

    def _collect_from_records(
        self,
        rows: List[Dict[str, Any]],
        *,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        route_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        route_meta = route_meta or {}
        for row_idx, row in enumerate(rows):
            for raw, path_hint in _iter_json_candidates(row):
                self._capture_candidate(
                    raw=raw,
                    hint=f"{path_hint}:{str(raw)[:120]}",
                    channel="post-json",
                    base_url=base_url,
                    docs=docs,
                    listings=listings,
                    route_meta={**route_meta, "search_result_index": row_idx},
                )
            detail = _detail_url_from_row(row, base_url=base_url)
            if detail:
                listings.append(detail)
                detail_pdf = _candidate_from_detail_query(detail, base_url=base_url)
                if detail_pdf:
                    self._capture_candidate(
                        raw=detail_pdf,
                        hint=f"detail-query:{str(detail_pdf)[:120]}",
                        channel="detail-query",
                        base_url=base_url,
                        docs=docs,
                        listings=listings,
                        route_meta={**route_meta, "search_result_index": row_idx},
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

    def _collect_from_json(
        self,
        *,
        json_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
        route_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        route_meta = route_meta or {}
        try:
            payload = json.loads(json_text or "{}")
        except Exception:
            return
        for raw, path_hint in _iter_json_candidates(payload):
            self._capture_candidate(
                raw=raw,
                hint=f"{path_hint}:{str(raw)[:120]}",
                channel="post-json",
                base_url=base_url,
                docs=docs,
                listings=listings,
                route_meta=route_meta,
            )
        records = _records_from_asmx_json(json_text)
        if records:
            self._collect_from_records(records, base_url=base_url, docs=docs, listings=listings, route_meta=route_meta)

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
        normalized = normalize_ihc_public_url(raw, base_url=base_url)
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
            detail_pdf = _candidate_from_detail_query(safe, base_url=base_url)
            if detail_pdf:
                try:
                    detail_pdf_safe = check_url_policy(
                        detail_pdf,
                        self.source.allow_list or [],
                        document_cdn_hosts=self.source.document_cdn_hosts or [],
                        allow_private_for_tests=_tests_allow_private(),
                    )
                except URLPolicyError:
                    self.stats["rejected_urls"] += 1
                    return
                docs.setdefault(
                    detail_pdf_safe,
                    {
                        "discovery_channel": f"{channel}:detail-query",
                        "discovery_hint": hint[:240],
                        **route_meta,
                    },
                )

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


def posting_final_url(posted_final_url: str, fallback_url: str) -> str:
    return posted_final_url or fallback_url


async def scrape_islamabad_high_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=IslamabadHighCourtPipeline,
        **kwargs,
    )
