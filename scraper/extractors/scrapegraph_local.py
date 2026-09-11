"""
MODE B — self-hosted / local ScrapeGraphAI (Amendment §2).

Two on-prem back-ends behind one engine:
  * `scrapegraphai` (the open-source library) when it is installed and SGAI_LOCAL_LLM_PROVIDER
    is 'scrapegraphai'; it is configured against the local model endpoint.
  * A direct JSON-mode call to the local model endpoint (Ollama `/api/chat` or any
    OpenAI-compatible `/v1/chat/completions`) — no third-party dependency, same fixed prompt,
    same schema coercion. This is the default and what the tests exercise.

Login-session material may reach this engine only when the endpoint is private/loopback or in
SGAI_LOCAL_TRUSTED_HOSTS; otherwise the call is refused as a privacy violation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Tuple

import httpx

from scraper.config import settings
from scraper.extractors.scrapegraph_base import LOCAL_BREAKER, BaseScrapeGraphEngine, ExtractionInput, PrivacyViolation, local_endpoint_is_on_prem
from scraper.extractors.schemas import EXTRACTION_TYPES

logger = logging.getLogger(__name__)


class LocalScrapeGraphEngine(BaseScrapeGraphEngine):
    engine_mode = "local"
    breaker = LOCAL_BREAKER

    def __init__(self, *, base_url: Optional[str] = None, model: Optional[str] = None, provider: Optional[str] = None, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base_url = (base_url or settings.SGAI_LOCAL_LLM_BASE_URL).rstrip("/")
        self.model = model or settings.SGAI_LOCAL_LLM_MODEL
        self.provider = (provider or settings.SGAI_LOCAL_LLM_PROVIDER or "ollama").lower()
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(settings.SGAI_ENABLED and settings.SGAI_LOCAL_ENABLED and self.base_url and self.model)

    @property
    def status(self) -> str:
        if not settings.SGAI_LOCAL_ENABLED:
            return "DISABLED"
        if not (self.base_url and self.model):
            return "NOT CONFIGURED"
        return "CONFIGURED"

    async def health(self) -> Dict[str, Any]:
        if not self.configured:
            return {"status": self.status}
        try:
            async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
                if self.provider == "ollama":
                    r = await client.get(f"{self.base_url}/api/tags")
                else:
                    r = await client.get(f"{self.base_url}/v1/models")
                return {"status": "UP" if r.status_code == 200 else f"HTTP {r.status_code}", "model": self.model, "provider": self.provider}
        except Exception as exc:
            return {"status": "DOWN", "error": str(exc)[:200], "model": self.model, "provider": self.provider}

    def _assert_on_prem(self, inp: ExtractionInput) -> None:
        if not self.configured:
            raise RuntimeError("local ScrapeGraph engine is NOT CONFIGURED")
        if inp.is_login_session and not local_endpoint_is_on_prem(self.base_url):
            raise PrivacyViolation(f"local engine endpoint {self.base_url} is not an on-prem/private host; login_session text refused")

    async def _call(self, extraction_type: str, prompt: str, payload_text: str, inp: ExtractionInput) -> Tuple[Any, Dict[str, Any]]:
        self._assert_on_prem(inp)
        if self.provider == "scrapegraphai":
            return await asyncio.to_thread(self._call_library, extraction_type, prompt, payload_text)
        return await self._call_endpoint(prompt, payload_text)

    # ------------------------------------------------------------------ direct endpoint (default)
    async def _call_endpoint(self, prompt: str, payload_text: str) -> Tuple[Any, Dict[str, Any]]:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": payload_text},
        ]
        async with httpx.AsyncClient(timeout=settings.SGAI_TIMEOUT_SECONDS, transport=self._transport) as client:
            if self.provider == "ollama":
                r = await client.post(f"{self.base_url}/api/chat", json={"model": self.model, "messages": messages, "stream": False, "format": "json", "options": {"temperature": 0}})
                r.raise_for_status()
                body = r.json()
                content = (body.get("message") or {}).get("content", "")
                meta = {"prompt_tokens": body.get("prompt_eval_count"), "completion_tokens": body.get("eval_count"), "credits": 0.0}
                return content, meta
            headers = {}
            key = settings.LOCAL_TREATMENT_API_KEY.get_secret_value()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            r = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=headers,
                json={"model": self.model, "messages": messages, "temperature": 0, "response_format": {"type": "json_object"}},
            )
            r.raise_for_status()
            body = r.json()
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            usage = body.get("usage") or {}
            return content, {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"), "credits": 0.0}

    # ------------------------------------------------------------------ scrapegraphai library
    def _call_library(self, extraction_type: str, prompt: str, payload_text: str) -> Tuple[Any, Dict[str, Any]]:
        try:
            from scrapegraphai.graphs import SmartScraperGraph  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional dependency
            raise RuntimeError("scrapegraphai library is not installed; set SGAI_LOCAL_LLM_PROVIDER=ollama for the direct endpoint") from exc
        model = EXTRACTION_TYPES[extraction_type]
        config = {
            "llm": {"model": f"ollama/{self.model}" if "/" not in self.model else self.model, "base_url": self.base_url, "temperature": 0, "format": "json"},
            "verbose": False,
            "headless": True,
        }
        graph = SmartScraperGraph(prompt=prompt, source=payload_text, config=config, schema=model)
        result = graph.run()
        return result, {"credits": 0.0}
