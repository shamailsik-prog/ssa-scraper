"""
Supreme Court of Pakistan (PUBLIC judgments) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - judgment-search AJAX results are harvested via bounded form-style POST requests
  - candidate links are normalized onto the Supreme Court public domain
  - judgment document links must pass the `%PDF` gate before ingestion
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

PUBLIC_HOST = "www.supremecourt.gov.pk"
PUBLIC_HOST_ALIASES = (PUBLIC_HOST, "supremecourt.gov.pk")

DEFAULT_LISTINGS = [
    "https://www.supremecourt.gov.pk/judgements/",
    "https://www.supremecourt.gov.pk/judgement-search/",
]
DEFAULT_SEARCH_ENDPOINT = "/wp-content/plugins/my-plugin/online_judgments.php"
DEFAULT_SEARCH_MAX_POSTS = 8
DEFAULT_SEARCH_MAX_PAGES = 2
DEFAULT_SEARCH_PAGE_FIELDS = ("page",)
DEFAULT_REPORTED_VALUES = ("yes", "no")

DOC_HINT_RE = re.compile(r"(?i)(judg|order|appeal|petition|case|pld|scmr|vs\.?|v\.?\s)")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?(?:downloads_judgements|wp-content/uploads)/[^\"'<>]+?\.pdf(?:\?[^\"'<>]*)?)")
REL_LISTING_RE = re.compile(r"(?i)(/?(?:judgement-search|judgements)(?:/page/\d+)?/?(?:\?[^\"'<>\s]*)?)")
REL_AJAX_RE = re.compile(r"(?i)(/?wp-content/plugins/[^\"'<>]+/online_judgments\.php(?:\?[^\"'<>]*)?)")
LISTING_PATH_RE = re.compile(r"(?i)^/(judgement-search|judgements)(/page/\d+)?/?$")
AJAX_PATH_RE = re.compile(r"(?i)/wp-content/plugins/.+/online_judgments\.php$")
AJAX_ENDPOINT_RE = re.compile(r"""(?i)\burl\s*:\s*["']([^"']*online_judgments\.php[^"']*)["']""")
PAGINATION_FIELD_HINT_RE = re.compile(r"(?i)(^|_)(page|paged|pageno|page_no|offset|start|limit|length)($|_)")

JSON_FILE_KEYS = frozenset(
    {
        "casefilename",
        "filename",
        "file_name",
        "file",
        "filepath",
        "file_path",
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


def normalize_supreme_court_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize a discovered candidate to Supreme Court public-domain URL form."""
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
    if re.match(r"(?i)^[^/\\]+\.(pdf)(?:\?.*)?$", candidate):
        candidate = f"/downloads_judgements/{candidate}"
    if re.match(r"(?i)^(downloads_judgements|wp-content/uploads)/", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^(judgement-search|judgements)(?:/.*)?$", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^wp-content/plugins/.+/online_judgments\.php(?:\?.*)?$", candidate):
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
    for u in REL_AJAX_RE.findall(text):
        yield u.rstrip(");,")


def _classify_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    parts = urlsplit(url)
    path = parts.path or "/"
    low_path = path.lower()
    hint = (hint_text or "").lower()
    is_pdf = low_path.endswith(".pdf") or ".pdf" in low_path
    in_doc_dir = "/downloads_judgements/" in low_path or "/wp-content/uploads/" in low_path

    if is_pdf and (in_doc_dir or DOC_HINT_RE.search(low_path) or DOC_HINT_RE.search(hint)):
        return "judgment"
    if AJAX_PATH_RE.search(path):
        return None
    if LISTING_PATH_RE.search(path) or ("page=" in (parts.query or "").lower() and "judg" in low_path):
        return "listing"
    return None


def _looks_like_search_listing(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower().rstrip("/")
    if path != "/judgement-search":
        return False
    low = (html_text or "").lower()
    return "online_judgments.php" in low or "id=case_type" in low


def _extract_case_types(html_text: str) -> List[str]:
    out: List[str] = []
    m = re.search(r"(?is)<select[^>]*\bid=(?:['\"])?case_type(?:['\"])?[^>]*>(.*?)</select>", html_text or "")
    if not m:
        return out
    for option_match in re.finditer(r"(?is)<option[^>]*\bvalue=(?:['\"])?([^\"'>\s]*)(?:['\"])?[^>]*>", m.group(1)):
        value = option_match.group(1).strip()
        if not value:
            continue
        if value.lower().startswith("select"):
            continue
        out.append(value)
    return list(dict.fromkeys(out))


def _extract_pagination_fields(html_text: str) -> List[str]:
    out: List[str] = []
    for m in re.finditer(r"(?i)\bname=(?:['\"])?([a-zA-Z0-9_]+)(?:['\"])?", html_text or ""):
        name = m.group(1).strip()
        if PAGINATION_FIELD_HINT_RE.search(name):
            out.append(name)
    return list(dict.fromkeys(out))


def search_posts_for(source: ScraperSource, html_text: str) -> List[Dict[str, str]]:
    cfg = source.config_json or {}
    raw = cfg.get("search_posts")
    if isinstance(raw, list):
        out: List[Dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            payload = _sanitize_post_payload(item)
            if payload:
                out.append(payload)
        if out:
            return out

    case_types = _extract_case_types(html_text)
    if not case_types:
        case_types = [""]
    years = _search_years_for(cfg)
    reported_values = _string_list(cfg.get("search_reported_values")) or list(DEFAULT_REPORTED_VALUES)
    max_posts = _positive_int(cfg.get("search_post_limit"), default=DEFAULT_SEARCH_MAX_POSTS)

    generated: List[Dict[str, str]] = []
    for case_type in case_types[: max(1, min(3, len(case_types)))]:
        for year in years:
            for reported in reported_values:
                payload = _base_search_payload()
                payload.update({"case_type": case_type, "case_year": str(year), "reported": reported})
                generated.append(payload)
                if len(generated) >= max_posts:
                    return generated

    if generated:
        return generated

    fallback = _base_search_payload()
    return [{**fallback, "reported": "yes"}, {**fallback, "reported": "no"}]


def search_endpoint_for(source: ScraperSource, *, base_url: str, html_text: str) -> Optional[str]:
    cfg = source.config_json or {}
    endpoint = cfg.get("search_endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        return normalize_supreme_court_public_url(endpoint, base_url=base_url)
    m = AJAX_ENDPOINT_RE.search(html_text or "")
    if m:
        return normalize_supreme_court_public_url(m.group(1), base_url=base_url)
    return normalize_supreme_court_public_url(DEFAULT_SEARCH_ENDPOINT, base_url=base_url)


def search_pagination_for(source: ScraperSource, html_text: str) -> Tuple[List[str], List[int]]:
    cfg = source.config_json or {}
    fields = _string_list(cfg.get("search_page_fields"))
    if not fields:
        fields = _extract_pagination_fields(html_text) or list(DEFAULT_SEARCH_PAGE_FIELDS)
    max_pages = _positive_int(cfg.get("search_max_pages"), default=DEFAULT_SEARCH_MAX_PAGES)
    pages_raw = cfg.get("search_pages")
    pages: List[int] = []
    if isinstance(pages_raw, list):
        for item in pages_raw:
            num = _positive_int(item, default=0)
            if num > 0:
                pages.append(num)
    if not pages:
        pages = list(range(1, max_pages + 1))
    pages = sorted({p for p in pages if p > 0})
    if not pages:
        pages = [1]
    return (fields, pages)


def _expand_paginated_payloads(base: Dict[str, str], *, page_fields: Sequence[str], pages: Sequence[int]) -> Iterable[Tuple[Dict[str, str], Dict[str, Any]]]:
    yielded_base = False
    for page in pages:
        if page <= 1:
            if yielded_base:
                continue
            yielded_base = True
            yield (dict(base), {"search_page": 1})
            continue
        for field in page_fields:
            payload = dict(base)
            payload[field] = str(page)
            yield (payload, {"search_page": page, "search_page_field": field})
    if not yielded_base:
        yield (dict(base), {"search_page": 1})


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
                if item.lower().endswith(".pdf"):
                    yield (item, str(idx))
            elif isinstance(item, (dict, list)):
                for candidate, path in _iter_json_candidates(item):
                    yield (candidate, f"[{idx}].{path}")


def _base_search_payload() -> Dict[str, str]:
    return {
        "case_type": "",
        "case_number": "",
        "case_year": "",
        "author_judge": "",
        "doa": "",
        "keywords": "",
        "parties_name": "",
        "tagline": "",
        "citation": "",
        "SCCitation": "",
        "reported": "yes",
    }


def _sanitize_post_payload(raw: Dict[str, Any]) -> Dict[str, str]:
    base = _base_search_payload()
    out = dict(base)
    for key, value in raw.items():
        if value is None:
            continue
        out[str(key)] = str(value)
    return out


def _string_list(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _search_years_for(cfg: Dict[str, Any]) -> List[int]:
    raw = cfg.get("search_years")
    years: List[int] = []
    if isinstance(raw, list):
        for item in raw:
            year = _positive_int(item, default=0)
            if year >= 1900:
                years.append(year)
    if years:
        return sorted(set(years), reverse=True)
    now = datetime.now(timezone.utc).year
    return [now, now - 1]


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


class SupremeCourtPipeline(PublicPipeline):
    """Public pipeline with Supreme Court listing + POST judgment-search result discovery."""

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
            endpoint = search_endpoint_for(self.source, base_url=res.final_url, html_text=res.text)
            if endpoint:
                try:
                    safe_endpoint = check_url_policy(
                        endpoint,
                        self.source.allow_list or [],
                        document_cdn_hosts=self.source.document_cdn_hosts or [],
                        allow_private_for_tests=_tests_allow_private(),
                    )
                    await self._harvest_search_posts(
                        endpoint_url=safe_endpoint,
                        listing_url=res.final_url,
                        listing_html=res.text,
                        docs=docs,
                        listings=listings,
                    )
                except URLPolicyError:
                    self.stats["rejected_urls"] += 1

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

    async def _harvest_search_posts(
        self,
        *,
        endpoint_url: str,
        listing_url: str,
        listing_html: str,
        docs: Dict[str, Dict[str, Any]],
        listings: List[str],
    ) -> None:
        post_specs = search_posts_for(self.source, listing_html)
        page_fields, pages = search_pagination_for(self.source, listing_html)
        sent_payload_signatures: set[str] = set()
        seen_response_hashes: set[str] = set()

        for base_payload in post_specs:
            for payload, page_meta in _expand_paginated_payloads(base_payload, page_fields=page_fields, pages=pages):
                payload_sig = json.dumps(payload, sort_keys=True)
                if payload_sig in sent_payload_signatures:
                    continue
                sent_payload_signatures.add(payload_sig)
                try:
                    posted = await self.post_form(endpoint_url, data=payload)
                except URLPolicyError:
                    continue
                response_hash = sha256(posted.content).hexdigest()
                if response_hash in seen_response_hashes and int(page_meta.get("search_page", 1)) > 1:
                    continue
                seen_response_hashes.add(response_hash)
                meta = {
                    "listing_fetch": "post_form",
                    "search_endpoint": endpoint_url,
                    "search": {k: v for k, v in payload.items() if v not in ("", None)},
                    **page_meta,
                }
                text = posted.text or ""
                if posted.content_kind == "json" or text.lstrip().startswith(("{", "[")):
                    self._collect_from_json(
                        json_text=text,
                        base_url=posting_final_url(posted.final_url, endpoint_url),
                        docs=docs,
                        listings=listings,
                        route_meta=meta,
                    )
                else:
                    self._collect_from_html(
                        html_text=text,
                        base_url=posted.final_url,
                        docs=docs,
                        listings=listings,
                        route_meta=meta,
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
            normalized = normalize_supreme_court_public_url(raw, base_url=base_url)
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
            kind = _classify_discovered_url(safe, hint_text=hint)
            if kind == "judgment":
                docs.setdefault(
                    safe,
                    {
                        "discovery_channel": channel,
                        "discovery_hint": hint[:240],
                        **route_meta,
                    },
                )
            elif kind == "listing" and safe != base_url:
                listings.append(safe)

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
            payload = json.loads(json_text or "[]")
        except Exception:
            return

        for raw, path_hint in _iter_json_candidates(payload):
            normalized = normalize_supreme_court_public_url(raw, base_url=base_url)
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
            hint = f"{path_hint}:{str(raw)[:120]}"
            kind = _classify_discovered_url(safe, hint_text=hint)
            if kind == "judgment":
                docs.setdefault(
                    safe,
                    {
                        "discovery_channel": "post-json",
                        "discovery_hint": hint[:240],
                        **route_meta,
                    },
                )
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
            for key_name in ("listing_fetch", "search_endpoint", "search", "search_page", "search_page_field"):
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


async def scrape_supreme_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=SupremeCourtPipeline,
        **kwargs,
    )
