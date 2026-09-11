"""
Shared raw-first pipeline for PUBLIC sources (Amendment §6, §11, §12).

    discovery (HTTP/Playwright under robots + allow-list) → crawl_frontier rows →
    fetch → SHA-256 → source_provenance → raw bytes → staging → deterministic parser →
    ScrapeGraph (managed or local) if needed → validator → PDF/OCR → promote/quarantine

Every connector (superior courts, PakistanCode, legislatures, NasirLawSite) is a thin layer of
discovery rules on top of this module. Explicit blocks HALT the source; robots and allow-list
rejections are recorded, never worked around.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.extractors.hybrid_extractor import HybridExtractor
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.extractors.scrapegraph_managed import ManagedScrapeGraphEngine
from scraper.fetchers import FetchResult, HttpFetcher, pdf_text_with_ocr, record_provenance, stage_judgment, stage_statute
from scraper.models import CrawlFrontier, ScraperSource
from scraper.notify import notify
from scraper.parsers.text_cleaner import clean_html
from scraper.security import ExplicitBlock, URLPolicyError, check_url_policy

logger = logging.getLogger(__name__)

JUDGMENT_LINK_HINT = re.compile(r"(?i)(judg|judgement|judgment|order|decision|case|\.pdf$)")
STATUTE_LINK_HINT = re.compile(r"(?i)(act|ordinance|statute|law|code|rules|regulation|bill|gazette|notification|\.pdf$)")


def _slim(d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if d is None:
        return None
    out = dict(d)
    for k in ("full_text_candidate", "full_text"):
        if isinstance(out.get(k), str) and len(out[k]) > 2000:
            out[k] = out[k][:2000] + f"…[{len(d[k])} chars in raw_text]"
    return out


class PublicPipeline:
    def __init__(
        self,
        db: AsyncSession,
        source: ScraperSource,
        *,
        fetcher: Optional[HttpFetcher] = None,
        managed: Optional[ManagedScrapeGraphEngine] = None,
        local: Optional[LocalScrapeGraphEngine] = None,
        job_id=None,
    ):
        self.db = db
        self.source = source
        self.fetcher = fetcher
        self.managed = managed
        self.local = local
        self.job_id = job_id
        self.stats = {"discovered": 0, "fetched": 0, "staged": 0, "duplicates": 0, "quarantined": 0, "rejected_urls": 0, "halted": False, "errors": 0}

    def _extractor(self, prov_id=None, staging_id=None) -> HybridExtractor:
        kwargs = {"provenance_id": prov_id, "staging_id": staging_id}
        if self.managed is not None:
            kwargs["managed"] = self.managed
        if self.local is not None:
            kwargs["local"] = self.local
        return HybridExtractor(self.db, self.source, **kwargs)

    # ------------------------------------------------------------------ discovery
    def links_from_html(self, html: str, base_url: str, hint: re.Pattern) -> List[str]:
        soup = BeautifulSoup(html or "", "html.parser")
        out: List[str] = []
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"].strip())
            label = a.get_text(" ", strip=True)
            if hint.search(href) or hint.search(label):
                try:
                    out.append(check_url_policy(href, self.source.allow_list or [], document_cdn_hosts=self.source.document_cdn_hosts or [], allow_private_for_tests=_tests_allow_private()))
                except URLPolicyError:
                    self.stats["rejected_urls"] += 1
        return list(dict.fromkeys(out))

    async def enqueue(self, urls: Iterable[str], *, kind: str, route: Dict[str, Any]) -> int:
        """Write discovered URLs into crawl_frontier (tier 0 = public document frontier)."""
        added = 0
        for url in urls:
            key = f"{kind}:{url}"
            exists = (await self.db.execute(select(CrawlFrontier).where(CrawlFrontier.source_name == self.source.source_name, CrawlFrontier.tier == 0, CrawlFrontier.query_key == key))).scalars().first()
            if exists:
                continue
            self.db.add(CrawlFrontier(source_name=self.source.source_name, tier=0, query_key=key, query_json={"kind": kind, "url": url, "route": route}, cursor_json={}, priority=50))
            added += 1
        await self.db.flush()
        self.stats["discovered"] += added
        return added

    async def pending(self, limit: int) -> List[CrawlFrontier]:
        q = (
            select(CrawlFrontier)
            .where(CrawlFrontier.source_name == self.source.source_name, CrawlFrontier.tier == 0, CrawlFrontier.status.in_(["pending", "in_progress"]))
            .order_by(CrawlFrontier.priority.asc(), CrawlFrontier.created_at.asc())
            .limit(limit)
        )
        return list((await self.db.execute(q)).scalars().all())

    # ------------------------------------------------------------------ fetching
    async def fetch(self, url: str) -> FetchResult:
        assert self.fetcher is not None
        try:
            res = await self.fetcher.get(url)
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            raise
        except ExplicitBlock as exc:
            if exc.kind == "robots_disallow":
                self.stats["rejected_urls"] += 1
                raise URLPolicyError(str(exc))
            await self.halt(str(exc))
            raise
        self.stats["fetched"] += 1
        return res

    async def halt(self, reason: str) -> None:
        self.source.state = "HALTED"
        self.source.state_reason = reason[:1000]
        self.source.state_changed_at = datetime.now(timezone.utc)
        self.source.requires_admin_review = True
        self.stats["halted"] = True
        await self.db.flush()
        await notify(self.db, level="critical", code="SOURCE_HALTED", message=f"explicit block — {reason}; no evasion attempted; admin review required", source_name=self.source.source_name)

    # ------------------------------------------------------------------ ingestion
    async def ingest_judgment(self, res: FetchResult, *, route: Dict[str, Any], row_meta: Optional[Dict[str, Any]] = None) -> str:
        row_meta = row_meta or {}
        if res.is_pdf:
            pdf_prov = await record_provenance(self.db, source=self.source, url=res.final_url, content=res.content, content_kind="pdf", route=route, http_status=res.status_code, is_original_document=True, document_kind="original_pdf")
            text, ocr = pdf_text_with_ocr(res.content)
            prov = pdf_prov
            html = None
        else:
            prov = await record_provenance(self.db, source=self.source, url=res.final_url, content=res.content, content_kind=res.content_kind, route=route, http_status=res.status_code)
            html = res.text
            text = clean_html(html)
            pdf_prov = None
            ocr = False
            # an HTML judgment page may link to its original PDF: preserve the original bytes too
            for pdf_url in self.links_from_html(html, res.final_url, re.compile(r"(?i)\.pdf($|\?)"))[:1]:
                try:
                    pdf_res = await self.fetch(pdf_url)
                    if pdf_res.is_pdf:
                        pdf_prov = await record_provenance(self.db, source=self.source, url=pdf_res.final_url, content=pdf_res.content, content_kind="pdf", route=route, is_original_document=True, document_kind="original_pdf", parent=prov)
                        pdf_text, ocr = pdf_text_with_ocr(pdf_res.content)
                        if len(pdf_text) > len(text):
                            text = pdf_text
                except ExplicitBlock:
                    raise
                except Exception as exc:
                    logger.warning("%s: original PDF fetch failed %s: %s", self.source.source_name, pdf_url, exc)
        staging = await stage_judgment(self.db, source=self.source, prov=prov, raw_html=html, raw_text=text, url=res.final_url, route=route, job_id=self.job_id, pdf_prov=pdf_prov if pdf_prov is not prov else (pdf_prov if res.is_pdf else None), ocr_applied=ocr)
        if staging.reconciled_json is not None:
            self.stats["duplicates"] += 1
            return "duplicate"
        extractor = self._extractor(prov.id, staging.id)
        outcome = await extractor.extract_judgment(html=html, text=text, source_meta={**row_meta, "url": res.final_url}, content_hash=prov.content_hash)
        staging.deterministic_json = _slim(outcome.deterministic_json)
        staging.ai_json = _slim(outcome.ai_json)
        staging.reconciled_json = _slim(outcome.data)
        staging.extraction_engine = outcome.engine
        staging.confidence_score = outcome.confidence
        staging.validation_errors = outcome.errors + [c.get("reason", "") for c in outcome.conflicts]
        staging.extracted_citation = (outcome.data.get("citations") or [None])[0]
        staging.extracted_title = outcome.data.get("case_title")
        staging.extracted_court = outcome.data.get("court")
        staging.extracted_year = outcome.data.get("year")
        staging.status = "quarantined" if outcome.quarantine else "extracted"
        staging.quarantine_reason = outcome.quarantine_reason
        if outcome.quarantine:
            self.stats["quarantined"] += 1
        self.stats["staged"] += 1
        await self.db.flush()
        return "staged"

    async def ingest_statute(self, res: FetchResult, *, route: Dict[str, Any], kind: str = "statute", meta: Optional[Dict[str, Any]] = None) -> str:
        meta = dict(meta or {})
        if res.is_pdf:
            prov = await record_provenance(self.db, source=self.source, url=res.final_url, content=res.content, content_kind="pdf", route=route, http_status=res.status_code, is_original_document=True, document_kind="original_pdf")
            text, _ = pdf_text_with_ocr(res.content)
            html = None
        else:
            prov = await record_provenance(self.db, source=self.source, url=res.final_url, content=res.content, content_kind=res.content_kind, route=route, http_status=res.status_code)
            html = res.text
            text = clean_html(html)
        staging = await stage_statute(self.db, source=self.source, prov=prov, raw_html=html, raw_text=text, url=res.final_url, kind=kind, job_id=self.job_id)
        if staging.reconciled_json is not None:
            self.stats["duplicates"] += 1
            return "duplicate"
        extractor = self._extractor(prov.id, staging.id)
        meta.setdefault("url", res.final_url)
        meta.setdefault("source_name", self.source.source_name)
        if kind == "instrument":
            outcome = await extractor.extract_instrument(html=html, text=text, source_meta=meta, content_hash=prov.content_hash)
        else:
            outcome = await extractor.extract_statute(html=html, text=text, source_meta=meta, content_hash=prov.content_hash)
        staging.deterministic_json = _slim(outcome.deterministic_json)
        staging.ai_json = _slim(outcome.ai_json)
        staging.reconciled_json = outcome.data if kind == "statute" else _slim(outcome.data)
        staging.extraction_engine = outcome.engine
        staging.confidence_score = outcome.confidence
        staging.validation_errors = outcome.errors + [c.get("reason", "") for c in outcome.conflicts]
        staging.status = "quarantined" if outcome.quarantine else "extracted"
        staging.quarantine_reason = outcome.quarantine_reason
        if outcome.quarantine:
            self.stats["quarantined"] += 1
        self.stats["staged"] += 1
        await self.db.flush()
        return "staged"

    # ------------------------------------------------------------------ frontier drain
    async def drain(self, limit: int) -> None:
        processed: set = set()
        while len(processed) < limit:
            batch = [fr for fr in await self.pending(limit) if fr.id not in processed]
            if not batch:
                return
            for fr in batch:
                if len(processed) >= limit:
                    return
                processed.add(fr.id)
                if await self._drain_one(fr) == "halted":
                    return

    async def _drain_one(self, fr: CrawlFrontier) -> str:
        """Process one frontier row. Returns 'ok' or 'halted' (explicit block: stop the run)."""
        fr.status = "in_progress"
        fr.attempts += 1
        kind = fr.query_json.get("kind", "judgment")
        url = fr.query_json.get("url")
        route = {**(fr.query_json.get("route") or {}), "frontier": fr.query_key}
        try:
            res = await self.fetch(url)
            if res.verdict.kind in ("verification", "login"):
                fr.status = "retired"
                fr.last_error = f"page requires {res.verdict.kind}; public source has no login path"
            elif res.status_code >= 400:
                fr.status = "retired" if res.status_code in (404, 410) else "pending"
                fr.last_error = f"HTTP {res.status_code}"
            else:
                if kind == "judgment":
                    await self.ingest_judgment(res, route=route, row_meta=fr.query_json.get("meta") or {})
                elif kind in ("statute", "instrument"):
                    await self.ingest_statute(res, route=route, kind=kind, meta=fr.query_json.get("meta") or {})
                elif kind == "listing":
                    await self.handle_listing(res, fr)
                fr.status = "done"
                fr.last_error = None
                fr.last_run_at = datetime.now(timezone.utc)
        except URLPolicyError as exc:
            fr.status = "retired"
            fr.last_error = str(exc)[:1000]
        except ExplicitBlock:
            fr.status = "pending"
            await self.db.flush()
            return "halted"
        except Exception as exc:
            self.stats["errors"] += 1
            fr.last_error = str(exc)[:1000]
            fr.status = "pending" if fr.attempts < 3 else "retired"
            logger.exception("%s: frontier item failed %s", self.source.source_name, url)
        await self.db.flush()
        return "ok"

    async def handle_listing(self, res: FetchResult, fr: CrawlFrontier) -> None:
        """A listing page: discover document links (and the next listing page) into the frontier."""
        kind = fr.query_json.get("target_kind", "judgment")
        hint = JUDGMENT_LINK_HINT if kind == "judgment" else STATUTE_LINK_HINT
        links = [u for u in self.links_from_html(res.text, res.final_url, hint) if u != res.final_url]
        await self.enqueue(links, kind=kind, route={"listing": res.final_url})
        soup = BeautifulSoup(res.text, "html.parser")
        nxt = soup.find("a", string=re.compile(r"(?i)^\s*(next|›|»|>|older)\s*$")) or soup.find("a", rel="next")
        depth = int(fr.query_json.get("depth", 0))
        if nxt and nxt.get("href") and depth < int(self.source.crawl_max_depth or 2):
            nurl = urljoin(res.final_url, nxt["href"])
            try:
                nurl = check_url_policy(nurl, self.source.allow_list or [], allow_private_for_tests=_tests_allow_private())
                key = f"listing:{nurl}"
                exists = (await self.db.execute(select(CrawlFrontier).where(CrawlFrontier.source_name == self.source.source_name, CrawlFrontier.tier == 0, CrawlFrontier.query_key == key))).scalars().first()
                if not exists:
                    self.db.add(CrawlFrontier(source_name=self.source.source_name, tier=0, query_key=key, query_json={"kind": "listing", "url": nurl, "target_kind": kind, "depth": depth + 1}, cursor_json={}, priority=40))
            except URLPolicyError:
                self.stats["rejected_urls"] += 1


def _tests_allow_private() -> bool:
    """Fixture servers in tests run on loopback; production never allows private hosts."""
    return bool(settings.APP_ENV == "development" and settings.DEBUG)


async def run_public_source(
    db: AsyncSession,
    source: ScraperSource,
    *,
    seed_listings: List[Dict[str, Any]],
    limit: int = 40,
    fetcher: Optional[HttpFetcher] = None,
    managed=None,
    local=None,
    job_id=None,
) -> Dict[str, Any]:
    """Generic public-source run: seed listing URLs into the frontier, then drain."""
    if source.state in ("HALTED", "DISABLED", "PAUSED"):
        return {"skipped": source.state, "reason": source.state_reason}
    pipeline = PublicPipeline(db, source, fetcher=fetcher, managed=managed, local=local, job_id=job_id)
    for item in seed_listings:
        url = item["url"]
        try:
            check_url_policy(url, source.allow_list or [], allow_private_for_tests=_tests_allow_private())
        except URLPolicyError as exc:
            logger.error("%s: seed URL rejected: %s", source.source_name, exc)
            continue
        key = f"listing:{url}"
        exists = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.source_name == source.source_name, CrawlFrontier.tier == 0, CrawlFrontier.query_key == key))).scalars().first()
        if exists is None:
            db.add(CrawlFrontier(source_name=source.source_name, tier=0, query_key=key, query_json={"kind": "listing", "url": url, "target_kind": item.get("target_kind", "judgment"), "depth": 0}, cursor_json={}, priority=30))
        elif exists.status == "done" and item.get("refresh", True):
            exists.status = "pending"
    await db.flush()
    own_fetcher = fetcher is None
    if own_fetcher:
        fetcher = HttpFetcher(source)
        pipeline.fetcher = fetcher
        await fetcher.__aenter__()
    try:
        await pipeline.drain(limit)
    finally:
        if own_fetcher:
            await fetcher.__aexit__(None, None, None)
    source.last_scraped_at = datetime.now(timezone.utc)
    if not pipeline.stats["halted"]:
        source.last_success_at = source.last_scraped_at
    source.total_pages_scraped += pipeline.stats["fetched"]
    source.total_records_extracted += pipeline.stats["staged"]
    await db.flush()
    return pipeline.stats
