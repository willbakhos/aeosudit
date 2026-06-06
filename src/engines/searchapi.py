"""SearchApi.io adapter for Google AI Mode.

AI Mode is Google's conversational AI search surface. It has a much
higher render rate than AI Overviews (which Apify's google-search-scraper
exposes), so for niche / long-tail buyer-intent queries that returned
empty from Apify, AI Mode typically returns rich content with the brand
names we score against.

Drop-in compatible with src.engines.apify.ApifyEngine — same Engine
contract, same EngineResponse shape, so industry_audit.py can switch
between them via a single feature-flag check.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.engines.base import Engine
from src.models import Citation, EngineResponse

SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"
DEFAULT_TIMEOUT = 60.0  # docs say ~4-5s typical; 60s is generous-but-bounded
MAX_RETRIES = 3
BASE_BACKOFF = 2.0

# country_code → SearchApi `location` string. Unknown codes get no location
# (AI Mode is globally available and defaults to US-style answers).
_COUNTRY_TO_LOCATION = {
    "us": "United States",
    "gb": "United Kingdom",
    "uk": "United Kingdom",
    "au": "Australia",
    "ca": "Canada",
}


class SearchApiEngine(Engine):
    """Google AI Mode via SearchApi.io. Returns concatenated AI-generated
    text in `response_text` plus parsed citations. Misses (no AI Mode
    rendered) come back as empty text + empty citations — the scoring
    pipeline already handles that case (brand match returns False → 0
    visibility for that query, same as before)."""

    def __init__(
        self,
        label: str,
        country_code: str = "us",
        language_code: str = "en",  # accepted for ApifyEngine parity; unused
        api_key: str | None = None,
    ):
        self.label = label
        self.country_code = country_code.lower()
        self.language_code = language_code.lower()
        self.api_key = api_key or os.environ.get("SEARCHAPI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("SEARCHAPI_API_KEY is not set")

    def _params(self, prompt: str) -> dict[str, str]:
        out: dict[str, str] = {
            "engine": "google_ai_mode",
            "q": prompt,
            "api_key": self.api_key,
        }
        loc = _COUNTRY_TO_LOCATION.get(self.country_code)
        if loc:
            out["location"] = loc
        return out

    async def query(self, prompt: str, query_type: str) -> EngineResponse:
        start = time.monotonic()
        try:
            data = await self._get_with_retry(self._params(prompt))
        except Exception as exc:  # noqa: BLE001
            # Redact: httpx's HTTPStatusError.__str__ embeds the full URL,
            # and our URL carries api_key=... as a query param. Never let
            # the key reach EngineResponse.error (which is persisted to
            # raw_responses.json + errors.log by the runner).
            return EngineResponse(
                engine_label=self.label,
                query=prompt,
                query_type=query_type,
                error=self._redact_key(f"{type(exc).__name__}: {exc}"),
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

    async def _get_with_retry(self, params: dict[str, str]) -> dict[str, Any]:
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.get(SEARCHAPI_URL, params=params)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        # Build a redacted error string so we never raise an
                        # httpx.HTTPStatusError whose __str__ embeds the
                        # api_key= query param. Caller catches all and
                        # already runs _redact_key, but defense-in-depth.
                        last_exc = RuntimeError(
                            f"HTTP {resp.status_code} from SearchApi: "
                            f"{resp.text[:200]}"
                        )
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(BASE_BACKOFF * (2**attempt))
                        continue
                    if resp.status_code >= 400:
                        # 4xx (non-429): raise a redacted error rather than
                        # resp.raise_for_status() which would leak the URL.
                        raise RuntimeError(
                            f"HTTP {resp.status_code} from SearchApi: "
                            f"{resp.text[:200]}"
                        )
                    return resp.json()
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(BASE_BACKOFF * (2**attempt))
        raise last_exc or RuntimeError("SearchApi request failed without exception")

    def _redact_key(self, s: str) -> str:
        """Replace this engine's api_key value anywhere in the string with
        '***REDACTED***'. Belt + suspenders — we already build error strings
        that don't embed the URL, but if any future code path stringifies
        an httpx exception that does, this catches it."""
        if not self.api_key:
            return s
        return s.replace(self.api_key, "***REDACTED***")

    @staticmethod
    def _parse(
        data: dict[str, Any],
    ) -> tuple[str, list[Citation], dict[str, Any]]:
        """Flatten AI Mode response into (text, citations, raw).

        text_blocks can be type=paragraph|header|unordered_list|code_blocks;
        every variant carries an `answer` field with the actual content the
        brand-name substring scorer needs. unordered_list nests bullets
        under `list[]` — those items also have answers we want.

        Returns ("", [], data) when the response has no AI Mode content
        (the empty-state signal — scoring naturally produces 0 for that query)."""
        if not isinstance(data, dict):
            return "", [], {}

        chunks: list[str] = []
        for b in data.get("text_blocks") or []:
            if not isinstance(b, dict):
                continue
            ans = (b.get("answer") or "").strip()
            if ans:
                chunks.append(ans)
            for item in (b.get("list") or []):
                if isinstance(item, dict):
                    i_ans = (
                        item.get("answer") or item.get("text") or ""
                    ).strip()
                    if i_ans:
                        chunks.append(i_ans)
        text = "\n\n".join(chunks)

        # Fall back to markdown if text_blocks was empty for some reason —
        # AI Mode normally returns both, but markdown is a safe fallback.
        if not text:
            text = (data.get("markdown") or "").strip()

        citations: list[Citation] = []
        for src in data.get("reference_links") or []:
            if not isinstance(src, dict):
                continue
            url = src.get("link") or ""
            if not url:
                continue
            netloc = urlparse(url).netloc.lower()
            domain = netloc[4:] if netloc.startswith("www.") else netloc
            if not domain:
                continue
            # SearchApi indices are 0-based; Citation.position is 1-based.
            try:
                pos = int(src.get("index", 0)) + 1
            except (TypeError, ValueError):
                pos = len(citations) + 1
            citations.append(
                Citation(
                    url=url,
                    title=src.get("title"),
                    domain=domain,
                    position=pos,
                )
            )

        return text, citations, data
