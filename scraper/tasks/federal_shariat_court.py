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
from typing import Any, Dict, Iterable, List, Optional, Tuple
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

DOC_HINT_RE = re.compile(r"(?i)(judg|order|appeal|petition|case)")
JUDGMENT_DIR_RE = re.compile(r"(?i)^/(judgments|judgements|judments)/")
WAYBACK_RE = re.compile(r"/web/\d+[a-z_]{0,6}/(https?://.+)$", re.I)
ABS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
REL_DOC_RE = re.compile(r"(?i)(/?(?:judgments|judgements|judments)/[^\"'<>]+)")
REL_LISTING_RE = re.compile(r"(?i)((?:/?(?:alljud\.php(?:\?[^\"'<>\s]*)?|judnew\d*\.html|en/(?:judgments|orders|leading-judgements)(?:/page/\d+)?/?)))")
LISTING_PATH_RE = re.compile(r"(?i)(/alljud\.php|/judnew\d*\.html|/en/judgments/?|/en/judgments/page/\d+/?|/en/orders/?|/en/orders/page/\d+/?|/en/leading-judgements/?|/en/leading-judgements/page/\d+/?)")


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    urls = cfg.get("listings") or DEFAULT_LISTINGS
    return [{"url": u, "target_kind": "judgment"} for u in urls]


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
    if "alljud.php" in path or re.search(r"/judnew\d*\.html$", path) or LISTING_PATH_RE.search(path) or re.search(r"/judgments/page/\d+/?$", path) or ("page=" in query and "alljud.php" in path):
        return "listing"
    if in_judgment_dir and not path.endswith((".html", ".php", "/")):
        return "judgment"
    return None


class FederalShariatCourtPipeline(PublicPipeline):
    """Public pipeline with FSC-specific robust listing discovery."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        docs: Dict[str, Dict[str, Any]] = {}
        listings: List[str] = []

        for raw, channel, hint in _iter_discovery_candidates(res.text):
            normalized = normalize_fsc_public_url(raw, base_url=res.final_url)
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
                docs.setdefault(safe, {"discovery_channel": channel, "discovery_hint": hint[:240]})
            elif kind == "listing" and safe != res.final_url:
                listings.append(safe)

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
            self.db.add(
                CrawlFrontier(
                    source_name=self.source.source_name,
                    tier=0,
                    query_key=key,
                    query_json={
                        "kind": "judgment",
                        "url": url,
                        "route": {"listing": listing_url},
                        "meta": meta,
                    },
                    cursor_json={},
                    priority=50,
                )
            )
            added += 1
        await self.db.flush()
        return added


async def scrape_federal_shariat_court(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    return await run_public_source(
        db,
        source,
        seed_listings=listings_for(source),
        pipeline_cls=FederalShariatCourtPipeline,
        **kwargs,
    )
