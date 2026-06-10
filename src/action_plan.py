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
            # minItems / maxItems removed: OpenRouter routes Claude through
            # providers (notably Google Vertex) whose schema engine only
            # accepts minItems values of 0 or 1, and rejected our 8 with
            # a 400. The prompt already asks for ~10 recommendations, so
            # bounding the array via JSON schema was belt-and-braces that
            # cost us the entire strict-mode call.
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
        "could action in the next 30 to 60 days to improve visibility and "
        "citations across AI engines.\n\n"
        "STYLE RULES (strict, non-negotiable):\n"
        "* NEVER use em dashes (U+2014) or en dashes (U+2013). Use commas, "
        "  parentheses, or rewrite the sentence. This is a hard requirement.\n"
        "* For numeric ranges, write '30 to 60' or '30-60' with a plain hyphen. "
        "  Never '30–60'.\n"
        "* Use simple punctuation: comma, full stop, colon, regular hyphen.\n\n"
        "Constraints on each recommendation:\n"
        "* 'title': short and verb-led (e.g. 'Publish a structured FAQ on loan eligibility')\n"
        "* 'category': one of content / schema / pr_outreach / site_structure / "
        "ground_truth / competitive\n"
        "* 'priority': high / medium / low (high means directly addresses the biggest visibility gap)\n"
        "* 'problem': 1 to 2 sentences naming the specific gap from the audit data (cite a query type, "
        "engine, or competitor where possible)\n"
        "* 'action': 2 to 4 sentences describing exactly what to do\n"
        "* 'example': REAL example copy/markup the team can adapt, not a "
        "placeholder. For schema, give actual JSON-LD. For content, write the "
        "actual paragraph or FAQ entry. For PR, name specific publication targets "
        "from the cited-domains list and draft a one-line pitch.\n"
        "* 'expected_impact': 1 sentence on what should improve and how to measure it\n\n"
        "Diversify across categories. Do not return 10 content recommendations. "
        "Lead with high-priority items.\n\n"
        f"AUDIT SUMMARY:\n{json.dumps(summary, indent=2)}\n"
    )


_DASH_REPLACEMENTS = [
    ("—", ", "),   # em dash
    ("–", "-"),    # en dash (usually a numeric range)
    ("―", ", "),   # horizontal bar
]


def _strip_dashes_deep(obj: Any) -> Any:
    """Recursively walk a parsed-JSON structure, stripping em/en/horizontal-
    bar dashes from any string leaf. User preference is "never use them"
    so we strip rather than ask the model again. Mirrors the same helper
    in industry_narrative.py."""
    if isinstance(obj, str):
        for needle, repl in _DASH_REPLACEMENTS:
            obj = obj.replace(needle, repl)
        return obj
    if isinstance(obj, list):
        return [_strip_dashes_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _strip_dashes_deep(v) for k, v in obj.items()}
    return obj


def _extract_content(data: dict[str, Any]) -> str:
    """Pull the text content out of an OpenRouter chat completion. Handles
    both string and Anthropic-style list-of-blocks responses."""
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    return content or ""


def _parse_recommendations(raw: str) -> list[dict[str, Any]]:
    """Pull the recommendations list out of the model's response. Tolerant
    of fenced code blocks (```json … ```) and chatter before/after the JSON
    object — both common when we drop strict mode and the model riffs."""
    s = (raw or "").strip()
    if not s:
        return []
    # Strip a leading ```json / ``` fence if present.
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3].rstrip()
    # Try direct parse first.
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        # Find the first { and last } and try that span.
        first = s.find("{")
        last = s.rfind("}")
        if first == -1 or last <= first:
            return []
        try:
            parsed = json.loads(s[first : last + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, list):
        return parsed
    return parsed.get("recommendations") or []


async def _post_once(
    client: httpx.AsyncClient, payload: dict[str, Any], api_key: str
) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/aeo-audit",
        "X-Title": "monitoraeo",
    }
    return await client.post(OPENROUTER_URL, headers=headers, json=payload)


async def _call_planner(
    summary: dict[str, Any], api_key: str
) -> list[dict[str, Any]]:
    """Two-stage call: try strict JSON-schema mode first (best quality on
    OpenAI models), fall back to plain prompt-instructed JSON if Claude on
    OpenRouter rejects the strict-mode payload or returns no recommendations.

    OpenRouter's strict-schema polyfill for Anthropic models is patchy —
    we've seen it return 400, return free-form text, or accept the call
    but silently produce {} with no 'recommendations' key. The loose
    fallback uses the same prompt with an explicit 'reply with JSON only'
    instruction, which all Claude models handle reliably."""
    prompt = _build_prompt(summary)

    strict_payload = {
        "model": PLANNER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
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

    loose_payload = {
        "model": PLANNER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": (
                    prompt
                    + "\n\nReply with ONLY a JSON object of shape "
                    + '{"recommendations": [...]}. No prose, no '
                    + "markdown code fences, no commentary — just the "
                    + "JSON. Each recommendation must include every "
                    + "field listed above."
                ),
            }
        ],
        "max_tokens": MAX_TOKENS,
    }

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            for mode, payload in (("strict", strict_payload), ("loose", loose_payload)):
                try:
                    resp = await _post_once(client, payload, api_key)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code} ({mode}): {resp.text[:200]}",
                            request=resp.request, response=resp,
                        )
                        # Retry the whole pair after backoff.
                        break
                    if resp.status_code >= 400:
                        # 4xx on strict mode → fall through to loose
                        # immediately (no point retrying a bad request).
                        # 4xx on loose → give up.
                        print(
                            f"[action_plan] {mode} mode rejected: "
                            f"HTTP {resp.status_code} {resp.text[:200]}"
                        )
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code} ({mode})",
                            request=resp.request, response=resp,
                        )
                        if mode == "strict":
                            continue  # try loose
                        break  # loose failed too → backoff + retry pair
                    data = resp.json()
                    recs = _parse_recommendations(_extract_content(data))
                    if recs:
                        if mode == "loose":
                            print(f"[action_plan] strict mode returned empty, loose mode produced {len(recs)}")
                        return recs
                    # Empty list — try loose if we're still on strict.
                    print(f"[action_plan] {mode} mode returned 0 recommendations")
                    if mode == "strict":
                        continue
                    # Loose also empty → backoff + retry pair.
                    break
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    break
            await asyncio.sleep(BASE_BACKOFF * (2**attempt))

    if last_exc:
        raise last_exc
    return []


def generate(
    rows: list[ScoredRow], config: SiteConfig
) -> list[dict[str, Any]]:
    """Top-level entry: returns a list of recommendation dicts. Empty list on
    failure (caller decides how to handle — the report is still useful without).

    Logs every failure mode loudly. Used to silently return [] which made the
    'action plan' sidebar section dim with no signal in the logs about why."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("[action_plan] BAIL: OPENROUTER_API_KEY not set — skipping action plan")
        return []
    if not rows:
        print("[action_plan] BAIL: no rows to summarise — skipping action plan")
        return []
    print(f"[action_plan] generating for {config.brand.name!r} from {len(rows)} rows using {PLANNER_MODEL}")
    summary = _summarise_findings(rows, config)
    try:
        recs = asyncio.run(_call_planner(summary, api_key))
        # Belt-and-suspenders: strip em/en/horizontal-bar dashes from
        # every string leaf even though the prompt forbids them. User
        # preference is "never use them" so we don't trust the model.
        recs = _strip_dashes_deep(recs)
        print(f"[action_plan] generated {len(recs)} recommendations")
        return recs
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[action_plan] FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return []
