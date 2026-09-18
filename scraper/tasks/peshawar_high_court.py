"""
Peshawar High Court (PUBLIC judgments) connector.

Discovery is intentionally strict:
  - allow-list + robots are always enforced by the shared pipeline
  - listing pages are discovered from anchors + data-* attributes + inline scripts
  - PHCCMS reported-judgment form results are harvested through bounded POST payloads
  - candidate links are normalized onto public PHC domains
  - judgment document links must pass the `%PDF` gate before ingestion
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, run_public_source

PUBLIC_HOST = "www.peshawarhighcourt.gov.pk"
PUBLIC_HOST_ALIASES = (
    PUBLIC_HOST,
    "peshawarhighcourt.gov.pk",
)

DEFAULT_LISTINGS = [
    "https://www.peshawarhighcourt.gov.pk/app/site/47/c/All_the_Referred,Reported_Judgments.html",
    "https://www.peshawarhighcourt.gov.pk/PHCCMS/reportedJudgments.php",
]

DEFAULT_SEARCH_YEAR_LIMIT = 1
DEFAULT_SEARCH_POSTS: List[Dict[str, str]] = [
    {
        "year": str(datetime.now(timezone.utc).year),
        "judge": "0",
        "category": "0",
        "txtsearchbyremarks": "",
        "submit": "search",
    }
]

DOC_HINT_RE = re.compile(r"(?i)(judg|judgement|judgment|order|appeal|petition|case|writ|pdf)")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?PHCCMS/+judgments/[^\"'<>\s]+?\.pdf(?:\?[^\"'<>\s]*)?)")
REL_LISTING_RE = re.compile(r"(?i)((?:\./|\.\./)*/?PHCCMS/reportedJudgments\.php(?:\?[^\"'<>\s]*)?)")
APP_LISTING_RE = re.compile(r"(?i)(/?app/site/\d+/c/[^\"'<>\s]*judgments[^\"'<>\s]*)")

SEARCH_PATH_RE = re.compile(r"(?i)^/PHCCMS/reportedJudgments\.php$")
DOC_PATH_RE = re.compile(r"(?i)^/PHCCMS/judgments/.+\.pdf$")


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("listings") or DEFAULT_LISTINGS
    return [{"url": u, "target_kind": "judgment"} for u in urls]


def normalize_phc_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize a discovered candidate to PHC public-domain URL form."""
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
    if re.match(r"(?i)^PHCCMS/", candidate):
        candidate = "/" + candidate
    if re.match(r"(?i)^app/site/", candidate):
        candidate = "/" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PUBLIC_HOST_ALIASES:
        scheme = "https"
        netloc = PUBLIC_HOST + (f":{parts.port}" if parts.port else "")
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    path = quote(path, safe="/%:@,+;=()-.~_")
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
    for u in APP_LISTING_RE.findall(text):
        yield u.rstrip(");,")


def _classify_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    parts = urlsplit(url)
    path = parts.path or "/"
    low_path = path.lower()
    hint = (hint_text or "").lower()
    is_pdf = low_path.endswith(".pdf") or ".pdf" in low_path
    if is_pdf and DOC_PATH_RE.search(path):
        return "judgment"
    if is_pdf and "/phccms/judgments/" in low_path and DOC_HINT_RE.search(hint):
        return "judgment"
    if SEARCH_PATH_RE.search(path):
        return "listing"
    if low_path.startswith("/app/site/") and "judgment" in low_path:
        return "listing"
    return None


def _looks_like_search_listing(url: str, html_text: str) -> bool:
    path = (urlsplit(url).path or "").lower()
    if path != "/phccms/reportedjudgments.php":
        return False
    low = (html_text or "").lower()
    return "name=\"year\"" in low and "name=\"judge\"" in low and "name=\"category\"" in low


def search_endpoint_for(source: ScraperSource, *, base_url: str, html_text: str) -> Optional[str]:
    cfg = source.config_json or {}
    endpoint = cfg.get("search_endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        return normalize_phc_public_url(endpoint, base_url=base_url)
    soup = BeautifulSoup(html_text or "", "html.parser")
    form = soup.find("form", action=re.compile(r"(?i)reportedJudgments\.php"))
    if form and form.get("action"):
        return normalize_phc_public_url(form.get("action", ""), base_url=base_url)
    return normalize_phc_public_url("/PHCCMS/reportedJudgments.php?action=search", base_url=base_url)


def search_posts_for(source: ScraperSource, *, html_text: str) -> List[Dict[str, str]]:
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
            post.setdefault("judge", "0")
            post.setdefault("category", "0")
            post.setdefault("txtsearchbyremarks", "")
            post.setdefault("submit", "search")
            out.append(post)
        if out:
            return out

    year_limit = _positive_int(cfg.get("search_years_limit"), default=DEFAULT_SEARCH_YEAR_LIMIT)
    years = _year_values_from_html(html_text)[:year_limit]
    if not years:
        return [dict(p) for p in DEFAULT_SEARCH_POSTS]
    return [
        {
            "year": year,
            "judge": "0",
            "category": "0",
            "txtsearchbyremarks": "",
            "submit": "search",
        }
        for year in years
    ]


def _year_values_from_html(html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    years: List[int] = []
    for option in soup.select("select[name='year'] option[value]"):
        value = (option.get("value") or "").strip()
        if not re.fullmatch(r"\d{4}", value):
            continue
        try:
            years.append(int(value))
        except ValueError:
            continue
    years = sorted(set(years), reverse=True)
    return [str(y) for y in years]


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


class PeshawarHighCourtPipeline(PublicPipeline):
    """Public pipeline with PHC listing + reported-judgments POST discovery."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sent_payload_signatures: set[str] = set()

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
                for payload in search_posts_for(self.source, html_text=res.text):
                    payload_sig = repr((endpoint, tuple(sorted(payload.items()))))
                    if payload_sig in self._sent_payload_signatures:
                        continue
                    self._sent_payload_signatures.add(payload_sig)
                    try:
                        posted = await self.post_form(endpoint, data=payload)
                    except URLPolicyError:
                        continue
                    safe_payload = {k: payload[k] for k in ("year", "judge", "category") if k in payload}
                    self._collect_from_html(
                        html_text=posted.text,
                        base_url=posted.final_url or endpoint,
                        docs=docs,
                        listings=listings,
                        route_meta={
                            "listing_fetch": "post_form",
                            "search_endpoint": endpoint,
                            "search": safe_payload,
                        },
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
    ) -> None:
        route_meta = route_meta or {}
        for raw, channel, hint in _iter_discovery_candidates(html_text):
            normalized = normalize_phc_public_url(raw, base_url=base_url)
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
            for key_name in ("listing_fetch", "search_endpoint", "search"):
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


async def scrape_peshawar_high_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=PeshawarHighCourtPipeline,
        **kwargs,
    )
