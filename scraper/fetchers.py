"""
Fetch layer and raw-first preservation primitives (Amendment §1-B, §6, §12).

    fetch (httpx or Playwright) → SHA-256 → preserve raw bytes → source_provenance row →
    staging row → ONLY THEN extraction.

`HttpFetcher` owns public HTTP fetches and streamed binary downloads under the source's
allow-list, robots policy and SSRF guard, with bounded retries/backoff. Playwright fetches are
produced by scraper.auth.session_manager and handed to the same preservation functions, so
every preserved page carries identical provenance regardless of the tool that fetched it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.models import ScraperSource, ScraperStaging, SourceProvenance, StatutesStaging
from scraper.security import ExplicitBlock, PageVerdict, check_url_policy, classify_response, robots_allows

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def canonical_text_hash(text: str) -> str:
    """Hash of whitespace-normalised text. Used to prove full_text is unchanged by extraction."""
    norm = " ".join((text or "").split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def has_pdf_signature(content: bytes) -> bool:
    """True when the payload looks like a real PDF (`%PDF` in the first KB)."""
    if not content:
        return False
    head = content[:1024]
    # Some servers prepend whitespace or a UTF-8 BOM before the PDF header.
    return head.lstrip().startswith(PDF_MAGIC) or PDF_MAGIC in head


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content: bytes
    content_type: str = ""
    elapsed_ms: int = 0
    verdict: PageVerdict = field(default_factory=lambda: PageVerdict("ok"))

    @property
    def text(self) -> str:
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError:
            return self.content.decode("latin-1", errors="replace")

    @property
    def is_pdf(self) -> bool:
        return self.content[:4] == PDF_MAGIC or "pdf" in (self.content_type or "").lower()

    @property
    def content_hash(self) -> str:
        return sha256_bytes(self.content)

    @property
    def content_kind(self) -> str:
        if self.is_pdf:
            return "pdf"
        ct = (self.content_type or "").lower()
        if "json" in ct:
            return "json"
        if "html" in ct or b"<html" in self.content[:2000].lower():
            return "html"
        return "text"


class HttpFetcher:
    """Public HTTP fetcher. Allow-list + SSRF + robots on every request; bounded retries."""

    def __init__(self, source: ScraperSource, *, client: Optional[httpx.AsyncClient] = None, resolver=None, allow_private_for_tests: bool = False):
        self.source = source
        self.allow_list = list(source.allow_list or [])
        self.cdn_hosts = list(source.document_cdn_hosts or [])
        self._client = client
        self._own_client = client is None
        self._resolver = resolver
        self._allow_private = allow_private_for_tests

    async def __aenter__(self) -> "HttpFetcher":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=settings.SCRAPER_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": settings.SCRAPER_USER_AGENT},
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()

    def _check(self, url: str) -> str:
        safe = check_url_policy(url, self.allow_list, document_cdn_hosts=self.cdn_hosts, resolver=self._resolver, allow_private_for_tests=self._allow_private)
        if self.source.respect_robots and not robots_allows(safe):
            raise ExplicitBlock("robots_disallow", safe)
        return safe

    async def _delay(self) -> None:
        lo = (self.source.request_delay_min_ms or 1200) / 1000.0
        hi = (self.source.request_delay_max_ms or 2500) / 1000.0
        await asyncio.sleep(random.uniform(lo, max(lo, hi)))

    async def get(self, url: str, *, max_bytes: Optional[int] = None, headers: Optional[Dict[str, str]] = None) -> FetchResult:
        assert self._client is not None, "use 'async with HttpFetcher(...)'"
        safe = self._check(url)
        attempts = max(1, settings.SCRAPER_RETRY_ATTEMPTS)
        last_exc: Optional[Exception] = None
        limit = max_bytes or settings.PDF_MAX_SIZE_MB * 1024 * 1024
        for attempt in range(attempts):
            started = datetime.now(timezone.utc)
            try:
                async with self._client.stream("GET", safe, headers=headers) as resp:
                    chunks = bytearray()
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise ValueError(f"response exceeds {limit} bytes")
                    content = bytes(chunks)
                    elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                    result = FetchResult(
                        url=safe,
                        final_url=str(resp.url),
                        status_code=resp.status_code,
                        content=content,
                        content_type=resp.headers.get("content-type", ""),
                        elapsed_ms=elapsed,
                    )
                # a redirect may have left the allow-list
                check_url_policy(result.final_url, self.allow_list, document_cdn_hosts=self.cdn_hosts, resolver=self._resolver, allow_private_for_tests=self._allow_private)
                body_text = result.text if result.content_kind in ("html", "text") else ""
                result.verdict = classify_response(result.status_code, body_text, result.final_url)
                if result.verdict.kind == "block":
                    raise ExplicitBlock(result.verdict.kind, result.verdict.detail)
                if result.status_code >= 500 and attempt + 1 < attempts:
                    await asyncio.sleep(settings.SCRAPER_RETRY_BACKOFF * (2**attempt))
                    continue
                await self._delay()
                return result
            except ExplicitBlock:
                raise
            except (httpx.TransportError, httpx.TimeoutException, ValueError) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(settings.SCRAPER_RETRY_BACKOFF * (2**attempt))
        raise RuntimeError(f"fetch failed for {safe}: {last_exc}")


# --------------------------------------------------------------------------- raw preservation
def raw_root() -> pathlib.Path:
    p = pathlib.Path(settings.RAW_STORAGE_PATH)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = pathlib.Path("./raw")
        p.mkdir(parents=True, exist_ok=True)
    return p


def preserve_raw_bytes(source_name: str, content: bytes, kind: str) -> str:
    """Write raw bytes once under RAW_STORAGE_PATH/<source>/<hh>/<hash>.<ext>; return the relative ref."""
    h = sha256_bytes(content)
    ext = {"pdf": "pdf", "html": "html", "json": "json"}.get(kind, "txt")
    rel = pathlib.Path(source_name) / h[:2] / f"{h}.{ext}"
    dest = raw_root() / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(dest)
    return str(rel)


def read_raw(ref: str) -> bytes:
    return (raw_root() / ref).read_bytes()


async def record_provenance(
    db: AsyncSession,
    *,
    source: ScraperSource,
    url: Optional[str],
    content: bytes,
    content_kind: str,
    route: Optional[Dict[str, Any]] = None,
    http_status: Optional[int] = None,
    is_original_document: bool = False,
    document_kind: Optional[str] = None,
    parent: Optional[SourceProvenance] = None,
) -> SourceProvenance:
    """Step 2–4 of the raw-first rule. Same content reached by another route → same row, route appended."""
    h = sha256_bytes(content)
    ref = preserve_raw_bytes(source.source_name, content, content_kind)
    existing = (
        await db.execute(select(SourceProvenance).where(SourceProvenance.source_name == source.source_name, SourceProvenance.content_hash == h))
    ).scalars().first()
    if existing is not None:
        routes = list(existing.routes or [])
        if route and route not in routes:
            routes.append(route)
            existing.routes = routes
        await db.flush()
        return existing
    prov = SourceProvenance(
        source_name=source.source_name,
        access_method=source.access_method,
        source_url=url,
        route_json=route or {},
        routes=[route] if route else [],
        content_hash=h,
        content_kind=content_kind,
        raw_ref=ref,
        byte_size=len(content),
        http_status=http_status,
        is_original_document=is_original_document,
        document_kind=document_kind or ("original_pdf" if content_kind == "pdf" else ("json" if content_kind == "json" else "html_text")),
        parent_id=parent.id if parent is not None else None,
    )
    db.add(prov)
    await db.flush()
    return prov


async def stage_judgment(
    db: AsyncSession,
    *,
    source: ScraperSource,
    prov: SourceProvenance,
    raw_html: Optional[str],
    raw_text: str,
    url: Optional[str],
    route: Optional[Dict[str, Any]] = None,
    job_id=None,
    pdf_prov: Optional[SourceProvenance] = None,
    ocr_applied: bool = False,
) -> ScraperStaging:
    """Step 5 of the raw-first rule. Duplicate content hash → existing staging row is returned."""
    existing = (
        await db.execute(select(ScraperStaging).where(ScraperStaging.source_name == source.source_name, ScraperStaging.content_hash == prov.content_hash))
    ).scalars().first()
    if existing is not None:
        return existing
    row = ScraperStaging(
        source_id=source.id,
        source_name=source.source_name,
        access_method=source.access_method,
        source_url=url,
        provenance_id=prov.id,
        job_id=job_id,
        content_hash=prov.content_hash,
        raw_ref=prov.raw_ref,
        raw_html=raw_html,
        raw_text=raw_text,
        raw_text_hash=canonical_text_hash(raw_text),
        pdf_provenance_id=pdf_prov.id if pdf_prov is not None else None,
        ocr_applied=ocr_applied,
        route_json=route or {},
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


async def stage_statute(
    db: AsyncSession,
    *,
    source: ScraperSource,
    prov: SourceProvenance,
    raw_html: Optional[str],
    raw_text: str,
    url: Optional[str],
    kind: str = "statute",
    job_id=None,
) -> StatutesStaging:
    existing = (
        await db.execute(select(StatutesStaging).where(StatutesStaging.source_name == source.source_name, StatutesStaging.content_hash == prov.content_hash))
    ).scalars().first()
    if existing is not None:
        return existing
    row = StatutesStaging(
        source_id=source.id,
        source_name=source.source_name,
        access_method=source.access_method,
        source_url=url,
        provenance_id=prov.id,
        job_id=job_id,
        content_hash=prov.content_hash,
        raw_ref=prov.raw_ref,
        raw_html=raw_html,
        raw_text=raw_text,
        kind=kind,
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


def pdf_text_with_ocr(pdf_bytes: bytes) -> tuple[str, bool]:
    """Extract text from original PDF bytes; returns (text, ocr_applied)."""
    from scraper.parsers.pdf_extractor import _pdfplumber_extract, extract_pdf_text  # type: ignore

    try:
        primary, _pages, scanned = _pdfplumber_extract(pdf_bytes)
    except Exception:
        primary, scanned = "", True
    text = extract_pdf_text(pdf_bytes)
    ocr_applied = bool(scanned or len((primary or "").strip()) < 200)
    return text, ocr_applied
