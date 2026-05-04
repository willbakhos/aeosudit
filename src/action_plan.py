"""Action-plan generator: turns audit findings into concrete recommendations
with example copy, via Claude Sonnet on OpenRouter. Used by the
'action_plan' tier ($349)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from src.models import ScoredRow, SiteConfig

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PLANNER_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_TIMEOUT = 180.0
MAX_RETRIES = 3
BASE_BACKOFF = 2.0
MAX_TOKENS = 6000

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "minItems": 8,
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "content",
                            "schema",
                            "pr_outreach",
                            "site_structure",
                            "ground_truth",
                            "competitive",
                        ],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "problem": {"type": "string"},
                    "action": {"type": "string"},
                    "example": {"type": "string"},
                    "expected_impact": {"type": "string"},
                },
                "required": [
                    "title",
                    "category",
                    "priority",
                    "problem",
                    "action",
                    "example",
                    "expected_impact",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


def _summarise_findings(rows: list[ScoredRow], config: SiteConfig) -> dict[str, Any]:
    """Compact summary of audit results — keeps the planner prompt small enough
    to fit comfortably under any model's context limit."""
    valid = [r for r in rows if not r.response.error]
    total = len(valid)
    visible = sum(1 for r in valid if r.deterministic.mentioned)
    cited = sum(1 for r in valid if r.deterministic.cited_as_source)

    own = config.brand.domain.lower().lstrip(".")
    competitor_text: dict[str, int] = {}
    competitor_cite: dict[str, int] = {}
    domain_cite: dict[str, int] = {}
    queries_missing: list[dict[str, Any]] = []
    queries_winning: list[dict[str, Any]] = []
    hallucinations: list[dict[str, Any]] = []

    for r in valid:
        for c in r.deterministic.competitors_mentioned:
            competitor_text[c] = competitor_text.get(c, 0) + 1
        for c in r.deterministic.competitors_cited:
            competitor_cite[c] = competitor_cite.get(c, 0) + 1
        for d in r.deterministic.all_cited_domains:
            d_norm = d.lower()
            d_norm = d_norm[4:] if d_norm.startswith("www.") else d_norm
            if d_norm == own or d_norm.endswith("." + own):
                continue
            domain_cite[d_norm] = domain_cite.get(d_norm, 0) + 1

        if r.deterministic.mentioned:
            queries_winning.append(
                {
                    "query": r.response.query,
                    "type": r.response.query_type,
                    "engine": r.response.engine_label,
                    "competitors_in_answer": r.deterministic.competitors_mentioned,
                }
            )
        else:
            queries_missing.append(
                {
                    "query": r.response.query,
                    "type": r.response.query_type,
                    "engine": r.response.engine_label,
                    "competitors_named_instead": r.deterministic.competitors_mentioned,
                }
            )

        if r.llm and r.llm.hallucination_flags:
            hallucinations.append(
                {
                    "query": r.response.query,
                    "engine": r.response.engine_label,
                    "false_claims": r.llm.hallucination_flags,
                }
            )

    return {
        "brand": {
            "name": config.brand.name,
            "domain": config.brand.domain,
            "aliases": config.brand.aliases,
            "ground_truth": config.ground_truth,
        },
        "competitors": config.competitors,
        "metrics": {
            "total_answers": total,
            "visibility_pct": round(100 * visible / total, 1) if total else 0.0,
            "citation_pct": round(100 * cited / total, 1) if total else 0.0,
        },
        "competitors_named_in_answers": sorted(
            competitor_text.items(), key=lambda kv: -kv[1]
        )[:10],
        "competitor_domains_cited": sorted(
            competitor_cite.items(), key=lambda kv: -kv[1]
        )[:10],
        "top_external_domains_cited": sorted(
            domain_cite.items(), key=lambda kv: -kv[1]
        )[:15],
        "queries_where_brand_missing": queries_missing[:30],
        "queries_where_brand_appears": queries_winning[:10],
        "hallucinations": hallucinations[:10],
    }


def _build_prompt(summary: dict[str, Any]) -> str:
    return (
        "You are an AEO (Answer Engine Optimisation) strategist. Below is a "
        "summarised audit of how AI answer engines treat a brand. Produce 10 "
        "specific, concrete recommendations that the brand's content + PR team "
        "could action in the next 30–60 days to improve visibility and "
        "citations across AI engines.\n\n"
        "Constraints on each recommendation:\n"
        "- 'title': short and verb-led (e.g. 'Publish a structured FAQ on loan eligibility')\n"
        "- 'category': one of content / schema / pr_outreach / site_structure / "
        "ground_truth / competitive\n"
        "- 'priority': high / medium / low — high = directly addresses the biggest visibility gap\n"
        "- 'problem': 1–2 sentences naming the specific gap from the audit data (cite a query type, "
        "engine, or competitor where possible)\n"
        "- 'action': 2–4 sentences describing exactly what to do\n"
        "- 'example': REAL example copy/markup the team can adapt — not a "
        "placeholder. For schema, give actual JSON-LD. For content, write the "
        "actual paragraph or FAQ entry. For PR, name specific publication targets "
        "from the cited-domains list and draft a one-line pitch.\n"
        "- 'expected_impact': 1 sentence on what should improve and how to measure it\n\n"
        "Diversify across categories — don't return 10 content recommendations. "
        "Lead with high-priority items.\n\n"
        f"AUDIT SUMMARY:\n{json.dumps(summary, indent=2)}\n"
    )


async def _call_planner(
    summary: dict[str, Any], api_key: str
) -> list[dict[str, Any]]:
    payload = {
        "model": PLANNER_MODEL,
        "messages": [{"role": "user", "content": _build_prompt(summary)}],
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "action_plan",
                "strict": True,
                "schema": ACTION_SCHEMA,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/aeo-audit",
        "X-Title": "monitoraeo",
    }

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post(OPENROUTER_URL, headers=headers, json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}",
                        request=resp.request,
                        response=resp,
                    )
                    await asyncio.sleep(BASE_BACKOFF * (2**attempt))
                    continue
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                parsed = json.loads(content)
                return parsed.get("recommendations", [])
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                await asyncio.sleep(BASE_BACKOFF * (2**attempt))

    if last_exc:
        raise last_exc
    return []


def generate(
    rows: list[ScoredRow], config: SiteConfig
) -> list[dict[str, Any]]:
    """Top-level entry: returns a list of recommendation dicts. Empty list on
    failure (caller decides how to handle — the report is still useful without)."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return []
    summary = _summarise_findings(rows, config)
    try:
        return asyncio.run(_call_planner(summary, api_key))
    except Exception:  # noqa: BLE001
        return []
