"""Apify adapter for Google AI Overviews / AI Mode."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.engines.base import Engine
from src.models import Citation, EngineResponse

APIFY_URL = (
    "https://api.apify.com/v2/acts/apify~google-search-scraper/"
    "run-sync-get-dataset-items"
)
DEFAULT_TIMEOUT = 180.0
MAX_RETRIES = 3
BASE_BACKOFF = 3.0


class ApifyEngine(Engine):
    def __init__(
        self,
        label: str,
        country_code: str = "us",
        language_code: str = "en",
        token: str | None = None,
    ):
        self.label = label
        self.country_code = country_code.lower()
        self.language_code = language_code.lower()
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        if not self.token:
            raise RuntimeError("APIFY_TOKEN is not set")

    def _payload(self, prompt: str) -> dict[str, Any]:
        return {
            "queries": prompt,
            "countryCode": self.country_code,
            "languageCode": self.language_code,
            "maxPagesPerQuery": 1,
            "resultsPerPage": 10,
            "includeAiOverview": True,
            "includeAiMode": True,
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
        text, citations, raw = self._parse(data)
        return EngineResponse(
            engine_label=self.label,
            query=prompt,
            query_type=query_type,
            response_text=text,
            citations=citations,
            raw_response=raw,
            latency_ms=latency_ms,
        )

    async def _post_with_retry(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        params = {"token": self.token}
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.post(APIFY_URL, params=params, json=payload)
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
        raise last_exc or RuntimeError("Apify request failed without exception")

    @staticmethod
    def _parse(
        data: list[dict[str, Any]],
    ) -> tuple[str, list[Citation], dict[str, Any]]:
        if not data:
            return "no AI Overview shown", [], {"items": []}
        item = data[0]
        ai_mode = item.get("aiModeResult") or item.get("aiOverview") or None
        if not ai_mode:
            return "no AI Overview shown", [], item

        text = ai_mode.get("text") or ai_mode.get("content") or ""
        sources = ai_mode.get("sources") or ai_mode.get("references") or []

        # Apify's "live" AI Overview returns content="" — Google rendered the
        # answer as per-source cards rather than a single text block. The card
        # titles + descriptions are the actual answer text, so synthesize one
        # so visibility scoring (does the answer mention the brand?) works.
        if not text.strip() and sources:
            synth: list[str] = []
            for src in sources:
                if not isinstance(src, dict):
                    continue
                title = (src.get("title") or src.get("name") or "").strip()
                desc = (
                    src.get("description")
                    or src.get("snippet")
                    or src.get("text")
                    or ""
                ).strip()
                if title and desc:
                    synth.append(f"{title}: {desc}")
                elif title or desc:
                    synth.append(title or desc)
            if synth:
                text = "\n\n".join(synth)

        citations: list[Citation] = []
        for idx, src in enumerate(sources, start=1):
            if not isinstance(src, dict):
                continue
            url = src.get("url") or src.get("link") or ""
            if not url:
                continue
            netloc = urlparse(url).netloc.lower()
            domain = netloc[4:] if netloc.startswith("www.") else netloc
            citations.append(
                Citation(
                    url=url,
                    title=src.get("title") or src.get("name"),
                    domain=domain,
                    position=idx,
                )
            )
        return text, citations, item
