"""Per-industry narrative generator. Takes the freshly-audited brand set
for one industry and asks Claude Sonnet for unique, data-grounded
observations: 2–3 narrative paragraphs, 4–6 FAQs, and short per-brand
context for the top 5.

Mirrors src/action_plan.py's strict-then-loose-JSON fallback because
OpenRouter's strict-schema polyfill for Anthropic models is unreliable.

Called from src/industry_audit.refresh_industry() at the end of every
monthly refresh. Cost: ~one Claude Sonnet call per industry per refresh,
roughly $0.04–0.07 depending on brand count + content length.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_TIMEOUT = 120.0
MAX_RETRIES = 2
BASE_BACKOFF = 2.0
MAX_TOKENS = 3500


def _summarise_for_prompt(
    industry_name: str,
    parent_category: str,
    brands: list[dict[str, Any]],
    top_cited_sources: list[str],
    movers: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build the compact data payload Sonnet sees. Caps lists so the
    prompt stays well under the 200k context window and the response
    stays in the ~3500 token budget we allocate."""
    return {
        "industry": industry_name,
        "parent_category": parent_category,
        "brand_count": len(brands),
        "top_brands": [
            {
                "rank": b["rank_in_industry"],
                "name": b["brand_name"],
                "domain": b["brand_domain"],
                "visibility_pct": round(b["visibility_pct"], 1),
                "citation_pct": round(b["citation_pct"], 1),
                "composite_score": round(b["visibility_score"], 1),
                "top_engine": b["top_engine"],
            }
            for b in brands[:10]
        ],
        "average_visibility_pct": round(
            sum(b["visibility_pct"] for b in brands) / len(brands), 1
        ) if brands else 0.0,
        "average_citation_pct": round(
            sum(b["citation_pct"] for b in brands) / len(brands), 1
        ) if brands else 0.0,
        "top_cited_sources_in_category": top_cited_sources[:8],
        "biggest_visibility_risers": movers.get("risers", [])[:3],
        "biggest_visibility_fallers": movers.get("fallers", [])[:3],
    }


def _build_prompt(summary: dict[str, Any]) -> str:
    industry = summary["industry"]
    return (
        f"You are an AEO (Answer Engine Optimisation) analyst. Below is fresh "
        f"audit data showing how Google AI Overviews describe and cite the top "
        f"brands in the '{industry}' category. Produce three things, grounded "
        f"strictly in the data — no fabricated facts, no generic AEO advice:\n\n"
        "1. 'narrative_paragraphs' — exactly 3 short paragraphs (60–90 words each) "
        "of OBSERVATIONS about this specific category's AI visibility landscape. "
        "Reference actual brand names, scores, and patterns from the data. "
        "Cover (a) who leads and why the gap matters, (b) where visibility "
        "diverges from citation (named vs trusted), (c) which sources/engines "
        "the data suggests the AI is anchoring on. No marketing language, no "
        "filler — every sentence should be a specific factual observation.\n\n"
        "2. 'brand_insights' — for the top 5 brands by rank, ONE sentence each "
        "(20–30 words) explaining their position relative to the others. "
        "Reference concrete numbers (their visibility vs the category average, "
        "their citation rate compared to peers, or their top engine if it's an "
        "outlier). No fluff.\n\n"
        "3. 'faqs' — 4–6 Question/Answer pairs about THIS specific category's "
        "AI visibility landscape. Questions should be queries a buyer/analyst "
        "might actually ask (\"Who leads AI visibility in [category]?\", "
        "\"What sources does AI cite most for [category] research?\"). Answers "
        "should be 1–2 sentences grounded in the data.\n\n"
        "Reply with ONLY a JSON object of shape "
        '{"narrative_paragraphs": [...], "brand_insights": [...], "faqs": [...]}. '
        "Each brand_insight is {rank, brand_name, text}. Each faq is {q, a}. "
        "No prose, no markdown fences, no commentary — JSON only.\n\n"
        f"AUDIT DATA:\n{json.dumps(summary, indent=2)}\n"
    )


def _extract_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""


def _parse_json_payload(raw: str) -> dict[str, Any]:
    """Tolerant JSON parser — strips code fences, finds the {...} span if
    the model added chatter. Returns {} on failure rather than raising."""
    s = (raw or "").strip()
    if not s:
        return {}
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3].rstrip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        first, last = s.find("{"), s.rfind("}")
        if first == -1 or last <= first:
            return {}
        try:
            parsed = json.loads(s[first : last + 1])
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


async def _post_once(client: httpx.AsyncClient, payload: dict[str, Any], api_key: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/aeo-audit",
        "X-Title": "monitoraeo",
    }
    return await client.post(OPENROUTER_URL, headers=headers, json=payload)


async def _call_narrator(summary: dict[str, Any], api_key: str) -> dict[str, Any]:
    """Single loose-JSON call (no strict schema — the prompt is explicit
    enough and Claude is reliable at instruction-following for this shape).
    Returns the parsed dict or {} on failure."""
    prompt = _build_prompt(summary)
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
    }
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await _post_once(client, payload, api_key)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}",
                        request=resp.request, response=resp,
                    )
                    await asyncio.sleep(BASE_BACKOFF * (2**attempt))
                    continue
                if resp.status_code >= 400:
                    print(f"[industry_narrative] rejected: HTTP {resp.status_code} {resp.text[:200]}")
                    return {}
                data = resp.json()
                raw = _extract_content(data)
                return _parse_json_payload(raw)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                await asyncio.sleep(BASE_BACKOFF * (2**attempt))
    if last_exc:
        print(f"[industry_narrative] gave up: {type(last_exc).__name__}: {last_exc}")
    return {}


def generate(
    industry_name: str,
    parent_category: str,
    brands: list[dict[str, Any]],
    top_cited_sources: list[str],
    movers: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Public entry point. Returns the narrative dict ready to store in
    IndustryReport.narrative. On any failure (missing API key, network,
    parse error) returns {} — caller stores the empty dict and the
    template falls back to showing the bare ranking table.

    `brands` is a list of dicts matching the IndustryBrand row shape with
    the keys this generator reads (brand_name, brand_domain, rank_in_industry,
    visibility_pct, citation_pct, visibility_score, top_engine).
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("[industry_narrative] OPENROUTER_API_KEY not set — skipping")
        return {}
    if not brands:
        return {}
    movers = movers or {"risers": [], "fallers": []}
    summary = _summarise_for_prompt(industry_name, parent_category, brands, top_cited_sources, movers)
    try:
        result = asyncio.run(_call_narrator(summary, api_key))
    except Exception as exc:  # noqa: BLE001
        print(f"[industry_narrative] generate failed: {type(exc).__name__}: {exc}")
        return {}
    if not result:
        return {}
    # Normalise shape: keep only the keys we use, drop anything else Claude
    # might add. Validate basic types.
    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "model": MODEL,
        "narrative_paragraphs": [
            p.strip() for p in (result.get("narrative_paragraphs") or [])
            if isinstance(p, str) and p.strip()
        ],
        "brand_insights": [
            {
                "rank": int(bi.get("rank") or 0),
                "brand_name": str(bi.get("brand_name") or "").strip(),
                "text": str(bi.get("text") or "").strip(),
            }
            for bi in (result.get("brand_insights") or [])
            if isinstance(bi, dict) and bi.get("text")
        ][:5],
        "faqs": [
            {
                "q": str(f.get("q") or "").strip(),
                "a": str(f.get("a") or "").strip(),
            }
            for f in (result.get("faqs") or [])
            if isinstance(f, dict) and f.get("q") and f.get("a")
        ][:6],
    }
    # Quality floor: if we got nothing usable, return empty so the template
    # falls back rather than rendering a half-empty narrative section.
    if not out["narrative_paragraphs"]:
        return {}
    return out
