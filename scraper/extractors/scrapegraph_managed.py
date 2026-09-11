"""
MODE A — managed ScrapeGraph API via the official `scrapegraph-py` SDK (Amendment §2).

PUBLIC material only. The `cookies`, `headers` and `stealth` parameters of the SDK are never
used. Every call is wrapped by the base class: privacy assertion, timeout, retries, circuit
breaker and schema coercion. The SDK surface is isolated in `_sdk_*` helpers so a v2 rename is
a one-file change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from scraper.config import settings
from scraper.extractors.schemas import EXTRACTION_TYPES
from scraper.extractors.scrapegraph_base import MANAGED_BREAKER, BaseScrapeGraphEngine, ExtractionInput, PrivacyViolation
from scraper.security import URLPolicyError, check_url_policy, robots_allows, scrub_secrets

logger = logging.getLogger(__name__)


def _default_client_factory():
    from scrapegraph_py import Client

    key = settings.SGAI_API_KEY.get_secret_value()
    if not key:
        raise RuntimeError("SGAI_API_KEY is NOT CONFIGURED")
    return Client(api_key=key)


class ManagedScrapeGraphEngine(BaseScrapeGraphEngine):
    engine_mode = "managed"
    breaker = MANAGED_BREAKER

    def __init__(self, client_factory: Optional[Callable[[], Any]] = None):
        self._client_factory = client_factory or _default_client_factory
        self._client = None

    @property
    def configured(self) -> bool:
        return settings.sgai_managed_configured or self._client_factory is not _default_client_factory

    def _client_instance(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # ------------------------------------------------------------------ SDK isolation
    def _sdk_extract(self, prompt: str, payload_text: str, output_model, input_kind: str, url: Optional[str]) -> Dict[str, Any]:
        client = self._client_instance()
        kwargs: Dict[str, Any] = {"user_prompt": prompt, "output_schema": output_model}
        if input_kind == "html":
            kwargs["website_html"] = payload_text
        elif input_kind == "markdown":
            kwargs["website_markdown"] = payload_text
        elif input_kind == "text":
            kwargs["website_markdown"] = payload_text
        elif url:
            kwargs["website_url"] = url
        else:
            kwargs["website_markdown"] = payload_text
        # Forbidden by policy and never passed: cookies, headers, stealth flags.
        return client.smartscraper(**kwargs)

    def _sdk_scrape(self, url: str, fmt: str) -> Dict[str, Any]:
        client = self._client_instance()
        if fmt == "markdown":
            return client.markdownify(website_url=url)
        return client.scrape(website_url=url)

    def _sdk_crawl(self, url: str, prompt: str, schema: Dict[str, Any], depth: int, max_pages: int, include_paths: Optional[List[str]]) -> Dict[str, Any]:
        client = self._client_instance()
        return client.crawl(url=url, prompt=prompt, data_schema=schema, depth=depth, max_pages=max_pages, same_domain_only=True, include_paths=include_paths)

    def _sdk_credits(self) -> Dict[str, Any]:
        return self._client_instance().get_credits()

    # ------------------------------------------------------------------ engine contract
    async def _call(self, extraction_type: str, prompt: str, payload_text: str, inp: ExtractionInput) -> Tuple[Any, Dict[str, Any]]:
        model = EXTRACTION_TYPES[extraction_type]
        raw = await asyncio.to_thread(self._sdk_extract, prompt, payload_text, model, inp.input_kind, inp.url)
        meta: Dict[str, Any] = {}
        if isinstance(raw, dict):
            meta["request_id"] = raw.get("request_id")
            usage = raw.get("usage") or {}
            meta["prompt_tokens"] = usage.get("prompt_tokens")
            meta["completion_tokens"] = usage.get("completion_tokens")
            meta["credits"] = raw.get("credits") or raw.get("credits_used") or 1.0
        return raw, meta

    # ------------------------------------------------------------------ public-page helpers
    async def scrape_public_url(self, url: str, allow_list: Iterable[str], *, fmt: str = "markdown", respect_robots: bool = True) -> Dict[str, Any]:
        """Managed fetch of a PUBLIC page. Refused when it would defeat robots or the allow-list."""
        safe = check_url_policy(url, allow_list)
        if respect_robots and not robots_allows(safe):
            raise URLPolicyError(f"robots.txt disallows {safe}")
        if self.breaker.is_open:
            raise RuntimeError("managed circuit breaker open")
        return await asyncio.to_thread(self._sdk_scrape, safe, fmt)

    async def crawl_public(self, url: str, allow_list: Iterable[str], *, max_depth: int, max_pages: int, include_paths: Optional[List[str]] = None) -> List[str]:
        """Managed crawl restricted to the source allow-list. Returns discovered URLs that pass the
        local policy check; the caller writes them into crawl_frontier — never a parallel corpus."""
        safe = check_url_policy(url, allow_list)
        if not robots_allows(safe):
            raise URLPolicyError(f"robots.txt disallows {safe}")
        schema = {"type": "object", "properties": {"links": {"type": "array", "items": {"type": "string"}}}}
        raw = await asyncio.to_thread(self._sdk_crawl, safe, "List every judgment or statute document link on the page.", schema, max_depth, max_pages, include_paths)
        urls: List[str] = []
        pages = []
        if isinstance(raw, dict):
            result = raw.get("result") or raw
            pages = result.get("pages") if isinstance(result, dict) and isinstance(result.get("pages"), list) else []
            if isinstance(result, dict) and isinstance(result.get("links"), list):
                pages.append({"links": result["links"]})
        for page in pages:
            for link in (page or {}).get("links") or []:
                try:
                    urls.append(check_url_policy(str(link), allow_list))
                except URLPolicyError as exc:
                    logger.info("crawl link rejected: %s", exc)
        return sorted(set(urls))

    async def credits(self) -> Dict[str, Any]:
        try:
            return await asyncio.to_thread(self._sdk_credits)
        except Exception as exc:
            return {"error": scrub_secrets(str(exc))}
