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
# Analyst narrative (below the ranking table): Claude Sonnet. Longer,
# more data-grounded, benefits from Claude's stricter instruction-following
# on the multi-key JSON contract this generator uses.
MODEL = "anthropic/claude-sonnet-4.6"
# Buyer-intent intro (above the ranking table): GPT-4o. The intro is short
# (~200 words), the prompt is simpler, and GPT-4o is materially cheaper
# than Claude Sonnet at the volume we run (382+ industries per backfill /
# monthly refresh cycle).
BUYER_INTRO_MODEL = "openai/gpt-4o"
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
    engine_name = summary.get("engine_name") or "Google AI"
    return (
        f"You are an AEO (Answer Engine Optimisation) analyst. Below is fresh "
        f"audit data showing how {engine_name} describes and cites the top "
        f"brands in the '{industry}' category. Produce three things, grounded "
        f"strictly in the data. No fabricated facts. No generic AEO advice.\n\n"
        "STYLE RULES (strict, non-negotiable):\n"
        "* NEVER use em dashes (U+2014) or en dashes (U+2013). Use commas, "
        "  parentheses, or rewrite the sentence. This is a hard requirement.\n"
        "* For numeric ranges, write '60 to 90' or '60-90' with a plain hyphen. "
        "  Never '60–90'.\n"
        "* Use simple punctuation: comma, full stop, colon, regular hyphen. "
        "  No fancy quotes, no ellipsis character, no em/en dashes.\n\n"
        "1. 'narrative_paragraphs': exactly 3 short paragraphs (60 to 90 words "
        "each) of OBSERVATIONS about this specific category's AI visibility "
        "landscape. Reference actual brand names, scores, and patterns from the "
        "data. Cover (a) who leads and why the gap matters, (b) where visibility "
        "diverges from citation (named vs trusted), (c) which sources/engines "
        "the data suggests the AI is anchoring on. No marketing language, no "
        "filler. Every sentence should be a specific factual observation.\n\n"
        "2. 'brand_insights': for the top 5 brands by rank, ONE sentence each "
        "(20 to 30 words) explaining their position relative to the others. "
        "Reference concrete numbers (their visibility vs the category average, "
        "their citation rate compared to peers, or their top engine if it's an "
        "outlier). No fluff.\n\n"
        "3. 'faqs': 4 to 6 Question/Answer pairs about THIS specific category's "
        "AI visibility landscape. Questions should be queries a buyer/analyst "
        "might actually ask (\"Who leads AI visibility in [category]?\", "
        "\"What sources does AI cite most for [category] research?\"). Answers "
        "should be 1 to 2 sentences grounded in the data.\n\n"
        "Reply with ONLY a JSON object of shape "
        '{"narrative_paragraphs": [...], "brand_insights": [...], "faqs": [...]}. '
        "Each brand_insight is {rank, brand_name, text}. Each faq is {q, a}. "
        "No prose, no markdown fences, no commentary. JSON only.\n\n"
        f"AUDIT DATA:\n{json.dumps(summary, indent=2)}\n"
    )


# Hard guardrail in case the model ignores the prompt rule. Replaces
# em/en/horizontal-bar dashes with safe substitutes. Tradeoff: the
# replacement isn't always grammatically perfect (an em dash often means
# a clause boundary that ', ' approximates better than ' - '), but the
# user's stated preference is "never use them" so erring on the side of
# strip is correct.
_DASH_REPLACEMENTS = [
    ("—", ", "),   # em dash
    ("–", "-"),    # en dash (usually a numeric range — plain hyphen fits)
    ("―", ", "),   # horizontal bar
]


def _strip_dashes(s: str) -> str:
    if not isinstance(s, str):
        return s
    out = s
    for needle, repl in _DASH_REPLACEMENTS:
        out = out.replace(needle, repl)
    return out


def _strip_dashes_deep(obj: Any) -> Any:
    """Recursively walk a parsed-JSON structure, stripping dashes from any
    string leaf. Idempotent: safe to run on already-clean data."""
    if isinstance(obj, str):
        return _strip_dashes(obj)
    if isinstance(obj, list):
        return [_strip_dashes_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _strip_dashes_deep(v) for k, v in obj.items()}
    return obj


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


def _build_buyer_intro_prompt(
    industry_name: str,
    parent_category: str,
    brands: list[dict[str, Any]],
    top_cited_sources: list[str],
    is_local: bool,
) -> str:
    """Prompt for the 2-3 paragraph buyer-intent intro rendered ABOVE the
    data on /ai-visibility/{slug}. Written for someone evaluating brands
    in this category, NOT for the analyst evaluating monitoraeo. The goal:
    the page has substantive text above the fold that earns clicks from
    Google for buyer queries like 'best X' or 'best X in [city]'."""
    audience = (
        "people looking to hire a provider in this category"
        if is_local
        else "people evaluating which brand to use in this category"
    )
    noun = "providers" if is_local else "brands"
    leader = brands[0].get("brand_name") if brands else None
    leader_v = brands[0].get("visibility_pct", 0) if brands else 0
    leader_c = brands[0].get("citation_pct", 0) if brands else 0
    summary = {
        "industry": industry_name,
        "parent_category": parent_category,
        "is_local_service": is_local,
        "brand_count": len(brands),
        "leader": leader,
        "leader_visibility_pct": round(leader_v, 1) if leader else None,
        "leader_citation_pct": round(leader_c, 1) if leader else None,
        "top_3_brands": [b.get("brand_name") for b in brands[:3]],
        "top_cited_sources": top_cited_sources[:5],
    }
    return (
        f"You are writing the introduction to a public-facing page about "
        f"{industry_name}. The audience is {audience}. The page below "
        f"the intro ranks {noun} by how often AI engines (ChatGPT, Claude, "
        f"Perplexity, Gemini, Google AI) name them in answers and cite "
        f"them as sources.\n\n"
        "STYLE RULES (strict, non-negotiable):\n"
        "* Write for the buyer, not the analyst. The reader cares about "
        "  picking a brand, not about monitoraeo's methodology.\n"
        "* NEVER use em dashes (U+2014) or en dashes (U+2013). Use commas, "
        "  parentheses, or rewrite the sentence. This is a hard requirement.\n"
        "* No marketing fluff. No 'in today's fast-moving world' openers. "
        "  Get to the substance in the first sentence.\n"
        "* Concrete > abstract. Name the leader, name the top sources, "
        "  reference real numbers from the data.\n"
        "* Plain hyphens for ranges (60 to 90, not 60–90).\n\n"
        "Produce exactly 3 short paragraphs (45 to 70 words each):\n"
        "1. Lead with what AI engines actually say when buyers ask about "
        f"{industry_name}. Name the leader. Mention 1-2 other top brands. "
        "Reference the visibility number for the leader if it is informative.\n"
        "2. Explain WHY the rankings look the way they do. Reference the "
        "top cited sources (the domains AI engines pull from most). If "
        "those are aggregator/review sites, say so and what that means.\n"
        "3. What this means for a buyer reading the page. One concrete "
        "thing to consider when picking from this list, framed around "
        "what AI engines value (recent reviews, third-party validation, "
        "topical authority on the specific subcategory).\n\n"
        "Reply with ONLY a JSON object of shape {\"paragraphs\": [str, str, str]}. "
        "No prose, no markdown fences, no commentary. JSON only.\n\n"
        f"DATA:\n{json.dumps(summary, indent=2)}\n"
    )


async def _call_buyer_intro(prompt: str, api_key: str) -> dict[str, Any]:
    """Same OpenRouter call shape as the analyst-narrative path. Buyer
    intros are short (~200 words total) so MAX_TOKENS can be smaller.
    Uses BUYER_INTRO_MODEL (GPT-4o) rather than the Claude MODEL that
    the analyst narrative uses, since the intro is simple and short-
    form and GPT-4o costs materially less at 382+ per refresh."""
    payload = {
        "model": BUYER_INTRO_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1200,
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
                    print(f"[buyer_intro] rejected: HTTP {resp.status_code} {resp.text[:200]}")
                    return {}
                data = resp.json()
                raw = _extract_content(data)
                return _parse_json_payload(raw)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                await asyncio.sleep(BASE_BACKOFF * (2**attempt))
    if last_exc:
        print(f"[buyer_intro] gave up: {type(last_exc).__name__}: {last_exc}")
    return {}


def generate_buyer_intro(
    industry_name: str,
    parent_category: str,
    brands: list[dict[str, Any]],
    top_cited_sources: list[str],
    is_local: bool = False,
) -> dict[str, Any]:
    """Generate the 2-3 paragraph buyer-intent intro for an industry page.
    Returns shape {generated_at, model, paragraphs: [str]} on success, {}
    on any failure (missing API key, empty brands, network, parse error).
    Caller stores the empty dict and the template falls back to no intro
    above the data, same as the analyst-narrative path."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("[buyer_intro] OPENROUTER_API_KEY not set, skipping")
        return {}
    if not brands:
        return {}
    prompt = _build_buyer_intro_prompt(
        industry_name, parent_category, brands, top_cited_sources, is_local,
    )
    try:
        result = asyncio.run(_call_buyer_intro(prompt, api_key))
    except Exception as exc:  # noqa: BLE001
        print(f"[buyer_intro] generate failed: {type(exc).__name__}: {exc}")
        return {}
    if not result:
        return {}
    result = _strip_dashes_deep(result)
    paragraphs = [
        p.strip() for p in (result.get("paragraphs") or [])
        if isinstance(p, str) and p.strip()
    ]
    if not paragraphs:
        return {}
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "model": BUYER_INTRO_MODEL,
        "paragraphs": paragraphs[:3],
    }


def generate(
    industry_name: str,
    parent_category: str,
    brands: list[dict[str, Any]],
    top_cited_sources: list[str],
    movers: dict[str, list[dict[str, Any]]] | None = None,
    engine_name: str | None = None,
) -> dict[str, Any]:
    """Public entry point. Returns the narrative dict ready to store in
    IndustryReport.narrative. On any failure (missing API key, network,
    parse error) returns {} — caller stores the empty dict and the
    template falls back to showing the bare ranking table.

    `brands` is a list of dicts matching the IndustryBrand row shape with
    the keys this generator reads (brand_name, brand_domain, rank_in_industry,
    visibility_pct, citation_pct, visibility_score, top_engine).

    `engine_name` flows into the prompt so the LLM knows which surface
    produced the data (Google AI Mode vs Google AI Overviews). Default
    "Google AI" stays correct regardless of the underlying backend.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("[industry_narrative] OPENROUTER_API_KEY not set — skipping")
        return {}
    if not brands:
        return {}
    movers = movers or {"risers": [], "fallers": []}
    summary = _summarise_for_prompt(industry_name, parent_category, brands, top_cited_sources, movers)
    summary["engine_name"] = engine_name or "Google AI"
    try:
        result = asyncio.run(_call_narrator(summary, api_key))
    except Exception as exc:  # noqa: BLE001
        print(f"[industry_narrative] generate failed: {type(exc).__name__}: {exc}")
        return {}
    if not result:
        return {}
    # Belt-and-suspenders: even with the prompt rule, the model
    # occasionally slips in an em or en dash. Walk every string leaf
    # before we shape the output. The user's stated preference is
    # "never use them" so we strip rather than ask the model again.
    result = _strip_dashes_deep(result)
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
