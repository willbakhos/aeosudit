"""OpenRouter adapter — one instance per model, all use native web search."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.engines.base import Engine
from src.models import Citation, EngineResponse

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT = 120.0
MAX_RETRIES = 4
BASE_BACKOFF = 2.0


class OpenRouterEngine(Engine):
    def __init__(self, model: str, label: str, api_key: str | None = None):
        self.model = model
        self.label = label
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/aeo-audit",
            "X-Title": "monitoraeo",
        }

    def _payload(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "type": "openrouter:web_search",
                    "openrouter:web_search": {
                        "engine": "native",
                        "max_results": 10,
                    },
                }
            ],
        }

    async def query(self, prompt: str, query_type: str) -> EngineResponse:
        start = time.monotonic()
        try:
            data = await self._post_with_retry(self._payload(prompt))
        except Exception as exc:  # noqa: BLE001
            return EngineResponse(
                engine_label=self.label,
                query=prompt,
                query_type=query_type,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        text, citations = self._parse(data)
        return EngineResponse(
            engine_label=self.label,
            query=prompt,
            query_type=query_type,
            response_text=text,
            citations=citations,
            raw_response=data,
            latency_ms=latency_ms,
        )

    async def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.post(
                        OPENROUTER_URL, headers=self._headers(), json=payload
                    )
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}: {resp.text[:200]}",
                            request=resp.request,
                            response=resp,
                        )
                        await asyncio.sleep(BASE_BACKOFF * (2**attempt))
                        continue
                    resp.raise_for_status()
                    return resp.json()
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    await asyncio.sleep(BASE_BACKOFF * (2**attempt))
        raise last_exc or RuntimeError("OpenRouter request failed without exception")

    @staticmethod
    def _parse(data: dict[str, Any]) -> tuple[str, list[Citation]]:
        choices = data.get("choices") or []
        if not choices:
            return "", []
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some models return content blocks
            text = "".join(
                block.get("text", "") for block in text if isinstance(block, dict)
            )

        citations: list[Citation] = []
        annotations = message.get("annotations") or []
        for idx, ann in enumerate(annotations, start=1):
            if not isinstance(ann, dict):
                continue
            if ann.get("type") != "url_citation":
                continue
            cite = ann.get("url_citation") or {}
            url = cite.get("url") or ""
            if not url:
                continue
            netloc = urlparse(url).netloc.lower()
            domain = netloc[4:] if netloc.startswith("www.") else netloc
            citations.append(
                Citation(
                    url=url,
                    title=cite.get("title"),
                    domain=domain,
                    position=idx,
                )
            )
        return text, citations
