"""Second-pass LLM scoring via Haiku on OpenRouter (sentiment + accuracy)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from tqdm.asyncio import tqdm_asyncio

from src.models import EngineResponse, LLMScore, SiteConfig

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SCORER_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 3
BASE_BACKOFF = 1.5
MAX_CONCURRENCY = 10
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sentiment": {
            "type": "string",
            "enum": ["positive", "neutral", "negative", "not_mentioned"],
        },
        "accuracy": {
            "type": "string",
            "enum": [
                "accurate",
                "partial",
                "contradicts_ground_truth",
                "not_applicable",
            ],
        },
        "hallucination_flags": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["sentiment", "accuracy", "hallucination_flags", "confidence"],
    "additionalProperties": False,
}


def _build_prompt(brand: str, ground_truth: list[str], response_text: str) -> str:
    gt = "\n".join(f"- {g}" for g in ground_truth) or "- (none provided)"
    return (
        f"Given this ground truth about {brand}:\n{gt}\n\n"
        f"And this AI-generated response:\n{response_text}\n\n"
        "Return JSON describing:\n"
        "- sentiment toward the brand: positive | neutral | negative | not_mentioned\n"
        "- factual accuracy vs ground truth: accurate | partial | "
        "contradicts_ground_truth | not_applicable (if brand is not mentioned)\n"
        "- hallucination_flags: list specific false claims about the brand\n"
        "- confidence: 0.0 to 1.0 in your assessment"
    )


async def _score_one(
    response: EngineResponse,
    config: SiteConfig,
    api_key: str,
    sem: asyncio.Semaphore,
) -> LLMScore:
    if response.error or not response.response_text:
        return LLMScore()

    prompt = _build_prompt(
        config.brand.name, config.ground_truth, response.response_text
    )
    payload = {
        "model": SCORER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "aeo_score",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/aeo-audit",
        "X-Title": "AEO Audit",
    }

    async with sem:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                last_exc: Exception | None = None
                for attempt in range(MAX_RETRIES):
                    try:
                        resp = await client.post(
                            OPENROUTER_URL, headers=headers, json=payload
                        )
                        if resp.status_code == 429 or resp.status_code >= 500:
                            last_exc = httpx.HTTPStatusError(
                                f"HTTP {resp.status_code}",
                                request=resp.request,
                                response=resp,
                            )
                            await asyncio.sleep(BASE_BACKOFF * (2**attempt))
                            continue
                        resp.raise_for_status()
                        data = resp.json()
                        break
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        last_exc = exc
                        await asyncio.sleep(BASE_BACKOFF * (2**attempt))
                else:
                    raise last_exc or RuntimeError("LLM scorer failed")
        except Exception:  # noqa: BLE001
            return LLMScore()

    try:
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        parsed = json.loads(content)
        return LLMScore(**parsed)
    except (KeyError, IndexError, json.JSONDecodeError, ValueError, TypeError):
        return LLMScore()


async def score_all(
    responses: list[EngineResponse],
    config: SiteConfig,
) -> list[LLMScore]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [_score_one(r, config, api_key, sem) for r in responses]
    return await tqdm_asyncio.gather(*tasks, desc="LLM scoring")
