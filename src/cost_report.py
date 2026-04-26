"""Internal cost report — breaks down API spend per engine, per query, per query type.

Reads raw_responses.json from a completed run; makes no API calls.
Run:
    python -m src.cost_report --run output/20260425_130531
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import typer
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.models import EngineResponse, LLMScore, ScoredRow, SiteConfig
from src.report import _aggregate
from src.scorer import score_response

# Apify google-search-scraper unit cost. The actor bills per 1k results;
# with includeAiOverview/AiMode the realised cost lands around ~$0.0035 per query.
APIFY_PER_QUERY_USD = 0.0035

# Per-call cost of the LLM scoring pass (Haiku). Roughly observed on past runs.
HAIKU_SCORING_PER_CALL_USD = 0.001

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

app = typer.Typer(add_completion=False)


def _usage(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {}
    return raw.get("usage") or {}


def _row_cost(r: dict[str, Any]) -> tuple[float, int, int, str]:
    """Return (cost_usd, prompt_tokens, completion_tokens, source)."""
    if r.get("error"):
        return 0.0, 0, 0, "error"
    raw = r.get("raw_response") or {}
    usage = raw.get("usage") or {}
    cost = usage.get("cost")
    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    if cost is not None:
        return float(cost), pt, ct, "openrouter.usage.cost"
    # Apify path: no usage.cost; flat per-query estimate
    return APIFY_PER_QUERY_USD, 0, 0, "apify.estimate"


def _section(title: str) -> str:
    return f"\n=== {title} ===\n"


def build_report(run_dir: Path) -> str:
    raw_path = run_dir / "raw_responses.json"
    if not raw_path.exists():
        raise FileNotFoundError(f"No raw_responses.json in {run_dir}")
    data = json.load(raw_path.open())

    by_engine: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"ok": 0, "err": 0, "cost": 0.0, "pt": 0, "ct": 0, "latency_ms": 0}
    )
    by_type: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"ok": 0, "err": 0, "cost": 0.0, "n": 0, "queries": set()}
    )
    by_query: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"ok": 0, "err": 0, "cost": 0.0, "n": 0}
    )

    grand_cost = 0.0
    grand_ok = 0
    grand_err = 0
    sources_used: set[str] = set()

    for r in data:
        eng = r.get("engine_label", "?")
        qtype = r.get("query_type", "?")
        query = r.get("query", "?")
        cost, pt, ct, source = _row_cost(r)
        sources_used.add(source)

        ok = not r.get("error")
        by_engine[eng]["cost"] += cost
        by_engine[eng]["pt"] += pt
        by_engine[eng]["ct"] += ct
        by_engine[eng]["latency_ms"] += r.get("latency_ms", 0) or 0
        if ok:
            by_engine[eng]["ok"] += 1
            grand_ok += 1
        else:
            by_engine[eng]["err"] += 1
            grand_err += 1

        by_type[qtype]["cost"] += cost
        by_type[qtype]["n"] += 1
        by_type[qtype]["queries"].add(query)
        by_type[qtype]["ok" if ok else "err"] += 1

        by_query[query]["cost"] += cost
        by_query[query]["n"] += 1
        by_query[query]["ok" if ok else "err"] += 1
        grand_cost += cost

    out: list[str] = []
    out.append(f"Cost report — {run_dir.name}")
    out.append(f"Source folder: {run_dir}")
    out.append(f"Cost sources used: {', '.join(sorted(sources_used))}")
    out.append(
        "Note: OpenRouter cost is the exact 'usage.cost' returned per call. "
        f"Apify cost is an estimate at ${APIFY_PER_QUERY_USD:.4f} per query."
    )

    # ----- by engine -----
    out.append(_section("Cost by engine"))
    header = (
        f"{'engine':<22} {'ok':>4} {'err':>4} {'cost USD':>10} "
        f"{'$/ok call':>11} {'prompt tok':>11} {'compl tok':>10} "
        f"{'$/1k tok':>10} {'avg s':>7}"
    )
    out.append(header)
    out.append("-" * len(header))
    for eng in sorted(by_engine, key=lambda e: -by_engine[e]["cost"]):
        s = by_engine[eng]
        ok = s["ok"]
        per_call = s["cost"] / ok if ok else 0.0
        total_tok = s["pt"] + s["ct"]
        per_1k = (s["cost"] / total_tok * 1000) if total_tok else 0.0
        avg_s = (s["latency_ms"] / (ok + s["err"]) / 1000) if (ok + s["err"]) else 0.0
        out.append(
            f"{eng:<22} {ok:>4} {s['err']:>4} ${s['cost']:>9.4f} "
            f"${per_call:>10.4f} {s['pt']:>11} {s['ct']:>10} "
            f"${per_1k:>9.4f} {avg_s:>6.1f}s"
        )
    out.append("-" * len(header))
    out.append(
        f"{'TOTAL':<22} {grand_ok:>4} {grand_err:>4} ${grand_cost:>9.4f}"
    )

    # ----- by query type -----
    out.append(_section("Cost by query type"))
    type_header = (
        f"{'type':<14} {'queries':>8} {'ok':>4} {'err':>4} "
        f"{'cost USD':>10} {'$/query':>10}"
    )
    out.append(type_header)
    out.append("-" * len(type_header))
    for qt in sorted(by_type, key=lambda t: -by_type[t]["cost"]):
        s = by_type[qt]
        per = s["cost"] / s["n"] if s["n"] else 0.0
        out.append(
            f"{qt:<14} {s['n']:>8} {s['ok']:>4} {s['err']:>4} "
            f"${s['cost']:>9.4f} ${per:>9.4f}"
        )

    # ----- top 10 most expensive queries -----
    out.append(_section("Top 10 most expensive queries (across all engines)"))
    q_header = f"{'cost USD':>10} {'calls':>6} {'ok':>4} {'err':>4}  query"
    out.append(q_header)
    out.append("-" * (len(q_header) + 40))
    ranked = sorted(by_query.items(), key=lambda kv: -kv[1]["cost"])[:10]
    for q, s in ranked:
        out.append(
            f"${s['cost']:>9.4f} {s['n']:>6} {s['ok']:>4} {s['err']:>4}  {q}"
        )

    # ----- waste analysis -----
    err_cost_lower_bound = sum(
        APIFY_PER_QUERY_USD
        for r in data
        if r.get("error") and r.get("engine_label", "").lower().startswith("google")
    )
    out.append(_section("Waste analysis"))
    out.append(
        f"Failed calls: {grand_err} of {grand_ok + grand_err} "
        f"({grand_err / max(grand_ok + grand_err, 1) * 100:.1f}%)"
    )
    out.append(
        "Failed OpenRouter calls cost $0 (4xx/5xx are not billed by OpenRouter). "
        f"Failed Apify calls billed at estimate: ${err_cost_lower_bound:.4f}."
    )
    out.append(
        f"Per usable answer: ${grand_cost / max(grand_ok, 1):.4f} "
        f"(spend / ok calls)."
    )

    # ----- write outputs -----
    text = "\n".join(out) + "\n"

    # Top 10 most expensive queries
    top_queries_sorted = sorted(
        by_query.items(), key=lambda kv: -kv[1]["cost"]
    )[:10]
    top_queries_blob = [
        {
            "query": q,
            "calls": s["n"],
            "ok": s["ok"],
            "err": s["err"],
            "cost": round(s["cost"], 4),
        }
        for q, s in top_queries_sorted
    ]

    # Waste breakdown — categorise failed calls
    waste_breakdown: list[dict[str, Any]] = []
    waste_breakdown.append({
        "label": "OpenRouter 4xx (404 wrong model, 402 no credit)",
        "count": sum(
            1 for r in data
            if r.get("error") and not r.get("engine_label", "").lower().startswith("google")
        ),
        "cost": 0.0,
        "fix": (
            "Verify model IDs in site.yaml against openrouter.ai/models. "
            "Top up OpenRouter credit before next run."
        ),
    })
    waste_breakdown.append({
        "label": "Apify failures",
        "count": sum(
            1 for r in data
            if r.get("error") and r.get("engine_label", "").lower().startswith("google")
        ),
        "cost": round(err_cost_lower_bound, 4),
        "fix": "Check Apify console for actor errors; raise timeout if needed.",
    })

    json_blob = {
        "run": run_dir.name,
        "queries": len({r.get("query") for r in data}),
        "totals": {
            "cost_usd": round(grand_cost, 4),
            "ok_calls": grand_ok,
            "error_calls": grand_err,
            "cost_per_usable_answer_usd": round(
                grand_cost / max(grand_ok, 1), 4
            ),
        },
        "by_engine": {
            eng: {
                "ok": s["ok"],
                "err": s["err"],
                "cost_usd": round(s["cost"], 4),
                "prompt_tokens": s["pt"],
                "completion_tokens": s["ct"],
                "avg_latency_s": round(
                    s["latency_ms"] / max(s["ok"] + s["err"], 1) / 1000, 2
                ),
            }
            for eng, s in by_engine.items()
        },
        "by_query_type": {
            qt: {
                "queries": s["n"],  # total calls of this type
                "unique_queries": len(s["queries"]),
                "ok": s["ok"],
                "err": s["err"],
                "cost_usd": round(s["cost"], 4),
            }
            for qt, s in by_type.items()
        },
        "top_queries": top_queries_blob,
        "waste": {
            "billable_cost": round(err_cost_lower_bound, 4),
            "breakdown": waste_breakdown,
        },
        "notes": {
            "openrouter_cost_source": "raw_response.usage.cost (exact, billed)",
            "apify_cost_source": (
                f"flat estimate ${APIFY_PER_QUERY_USD:.4f}/query — Apify "
                "billing is per result and varies; check the Apify console "
                "for exact per-run charge."
            ),
            "failed_call_billing": (
                "OpenRouter 4xx/5xx are not billed. Apify 4xx/5xx may still "
                "incur partial cost; treat as estimate."
            ),
        },
    }

    return text, json_blob


@app.callback()
def _root() -> None:
    """Cost report."""


def _build_audit_style_context(
    run_dir: Path, blob: dict[str, Any], config: SiteConfig
) -> dict[str, Any]:
    """Build the same `agg` used by the audit report, plus a `costs` dict
    with per-section / per-engine / per-type / per-query / per-call dollar values."""
    raw_path = run_dir / "raw_responses.json"
    data = json.load(raw_path.open())

    # Reconstruct ScoredRows so the template can render the drill-down
    responses = [EngineResponse.model_validate(r) for r in data]
    rows = [
        ScoredRow(
            response=r,
            deterministic=score_response(r, config),
            llm=LLMScore(),
        )
        for r in responses
    ]
    agg = _aggregate(rows, config)

    # Per-call cost map keyed by "engine||query"
    by_call: dict[str, float] = {}
    by_query_cost: dict[str, float] = defaultdict(float)
    by_engine_cost: dict[str, float] = defaultdict(float)
    by_type_cost: dict[str, float] = defaultdict(float)
    total = 0.0
    ok = 0
    err = 0
    for r in data:
        cost, _, _, _ = _row_cost(r)
        eng = r.get("engine_label", "?")
        q = r.get("query", "?")
        qt = r.get("query_type", "?")
        by_call[f"{eng}||{q}"] = cost
        by_query_cost[q] += cost
        by_engine_cost[eng] += cost
        by_type_cost[qt] += cost
        total += cost
        if r.get("error"):
            err += 1
        else:
            ok += 1

    wasted = sum(
        cost for r in data
        for cost, *_ in [_row_cost(r)]
        if r.get("error")
    )
    haiku_cost = ok * HAIKU_SCORING_PER_CALL_USD

    def pct(x: float) -> float:
        return (x / total * 100) if total else 0.0

    sections = {
        "headline": {
            "cost": total,
            "pct": 100.0,
            "note": (
                "All 4 headline metrics are computed from the full set of API "
                "responses, so this section accounts for 100% of the run cost. "
                "It does not trigger any new calls — it summarises calls "
                "already made for the other sections."
            ),
        },
        "heatmap": {
            "cost": total,
            "pct": 100.0,
            "note": (
                "The heatmap groups every (query × engine) call into a "
                f"{len(agg['types'])}×{len(agg['engines'])} grid. Each column "
                "header below shows what that engine cost in total; each row "
                "header shows what that query type cost. Sum = run total."
            ),
        },
        "sources": {
            "cost": total - wasted,
            "pct": pct(total - wasted),
            "note": (
                "Built from the citations array on every successful response. "
                "Failed calls returned no citations, so this section's cost is "
                f"the run total minus the ${wasted:.4f} spent on failed calls."
            ),
        },
        "competitors": {
            "cost": total - wasted,
            "pct": pct(total - wasted),
            "note": (
                "Counts competitor names in answer text + competitor domains in "
                "citations. Same source data as Top cited sources, so same "
                "attributed cost."
            ),
        },
        "hallucinations": {
            "cost": haiku_cost,
            "pct": pct(haiku_cost),
            "note": (
                f"Adds an extra Haiku call per ok response (~${HAIKU_SCORING_PER_CALL_USD:.4f}). "
                f"For this run that's {ok} extra calls ≈ ${haiku_cost:.4f}. This "
                "is the only section with its own incremental cost — every other "
                "section reuses the audit calls. (Note: in this run all Haiku "
                "calls failed because OpenRouter credit was exhausted, so the "
                "real billed cost was $0.)"
            ),
        },
        "drilldown": {
            "cost": total,
            "pct": 100.0,
            "note": (
                "Renders the raw text + citations of every call. Each query "
                "below has its own cost pill, and each engine block within a "
                "query shows that single call's cost."
            ),
        },
    }

    costs = {
        "total": total,
        "wasted": wasted,
        "ok_calls": ok,
        "err_calls": err,
        "per_usable": (total / ok) if ok else 0.0,
        "by_call": by_call,
        "by_query": dict(by_query_cost),
        "by_engine": dict(by_engine_cost),
        "by_type": dict(by_type_cost),
        "sections": sections,
    }

    return {"config": config, "agg": agg, "costs": costs}


def _build_html_context(blob: dict[str, Any]) -> dict[str, Any]:
    """Reshape the JSON blob into the structure the template expects."""
    totals = blob["totals"]
    by_engine_raw = blob["by_engine"]
    by_type_raw = blob["by_query_type"]
    top_queries = blob["top_queries"]
    waste = blob["waste"]
    queries = blob["queries"]

    total_cost = totals["cost_usd"]
    ok = totals["ok_calls"]
    err = totals["error_calls"]
    total_calls = ok + err

    # by engine — rank by cost desc
    engines: list[dict[str, Any]] = []
    total_pt = total_ct = 0
    for eng, s in by_engine_raw.items():
        cost = s["cost_usd"]
        pt = s["prompt_tokens"]
        ct = s["completion_tokens"]
        total_pt += pt
        total_ct += ct
        engines.append({
            "engine": eng,
            "ok": s["ok"],
            "err": s["err"],
            "cost": cost,
            "cost_per_ok": cost / s["ok"] if s["ok"] else 0.0,
            "pct_total": (cost / total_cost * 100) if total_cost else 0.0,
            "pt": pt,
            "ct": ct,
            "cost_per_1k": (cost / (pt + ct) * 1000) if (pt + ct) else 0.0,
            "avg_s": s["avg_latency_s"],
        })
    engines.sort(key=lambda e: -e["cost"])

    # by query type — rank by cost desc
    types_list: list[dict[str, Any]] = []
    for qt, s in by_type_raw.items():
        cost = s["cost_usd"]
        n = s["queries"]  # this is calls in current build_report; treat as calls
        types_list.append({
            "type": qt,
            "queries": s["unique_queries"],
            "calls": n,
            "ok": s["ok"],
            "err": s["err"],
            "cost": cost,
            "cost_per_query": cost / s["unique_queries"] if s["unique_queries"] else 0.0,
            "pct_total": (cost / total_cost * 100) if total_cost else 0.0,
        })
    types_list.sort(key=lambda t: -t["cost"])

    top_total = sum(q["cost"] for q in top_queries)

    # Section costs — attribute spend to sections of the audit report
    section_costs = [
        {
            "name": "Headline metrics",
            "source": "Aggregate of all 200 calls",
            "cost": total_cost,
            "pct": 100.0,
        },
        {
            "name": "Engine heatmap",
            "source": "All calls grouped by query type × engine",
            "cost": total_cost,
            "pct": 100.0,
        },
        {
            "name": "Top cited sources",
            "source": "Citation arrays from all ok responses",
            "cost": total_cost - waste["billable_cost"],
            "pct": (total_cost - waste["billable_cost"]) / total_cost * 100 if total_cost else 0.0,
        },
        {
            "name": "Competitor share-of-voice",
            "source": "Text + citation scan of all ok responses",
            "cost": total_cost - waste["billable_cost"],
            "pct": (total_cost - waste["billable_cost"]) / total_cost * 100 if total_cost else 0.0,
        },
        {
            "name": "Hallucination flags",
            "source": "Adds Haiku LLM scoring pass on top of audit calls",
            "cost": ok * HAIKU_SCORING_PER_CALL_USD,
            "pct": (ok * HAIKU_SCORING_PER_CALL_USD) / total_cost * 100 if total_cost else 0.0,
        },
        {
            "name": "Per-query drill-down",
            "source": "Raw response text + citations from all calls",
            "cost": total_cost,
            "pct": 100.0,
        },
    ]

    # Recommendations sized off observed spend
    claude_cost = next((e["cost"] for e in engines if e["engine"].lower() == "claude"), 0.0)
    chatgpt_cost = next((e["cost"] for e in engines if e["engine"].lower() == "chatgpt"), 0.0)
    comparison_cost = next(
        (t["cost"] for t in types_list if t["type"].lower() == "comparison"), 0.0
    )
    recs = [
        {
            "lever": "Switch Claude to Sonnet",
            "description": (
                "Swap anthropic/claude-opus-4.7 → anthropic/claude-sonnet-4.6 in "
                "site.yaml. Sonnet is ~5× cheaper with similar AEO-relevant signal."
            ),
            "saving": round(claude_cost * 0.8, 4),
        },
        {
            "lever": "Drop comparison queries",
            "description": (
                "Comparison queries (e.g. 'X vs Y') are the most expensive query "
                "type; trim from queries.csv if not needed every run."
            ),
            "saving": round(comparison_cost * 0.7, 4),
        },
        {
            "lever": "Cache GPT-5 native search",
            "description": (
                "ChatGPT calls are ~80s avg and pull large prompt-token counts due "
                "to native search. Consider running GPT-5 quarterly, not every run."
            ),
            "saving": round(chatgpt_cost * 0.5, 4),
        },
        {
            "lever": "Skip LLM scoring on dry runs",
            "description": (
                "Pass --skip-llm-scoring for iterative testing; only run the full "
                "scorer pass when you intend to publish the report."
            ),
            "saving": round(ok * HAIKU_SCORING_PER_CALL_USD, 4),
        },
    ]

    return {
        "run_name": blob["run"],
        "c": {
            "total_cost": total_cost,
            "ok_calls": ok,
            "err_calls": err,
            "err_pct": (err / total_calls * 100) if total_calls else 0.0,
            "wasted_cost": waste["billable_cost"],
            "cost_per_usable": totals["cost_per_usable_answer_usd"],
            "queries": queries,
            "by_engine": engines,
            "by_type": types_list,
            "total_pt": total_pt,
            "total_ct": total_ct,
            "top_queries": top_queries,
            "top_total": top_total,
            "section_costs": section_costs,
            "waste": waste["breakdown"],
            "recommendations": recs,
            "savings_estimate": sum(r["saving"] for r in recs),
        },
    }


def write_html_report(
    run_dir: Path, blob: dict[str, Any], config: SiteConfig
) -> Path:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("cost_report.html.j2")
    ctx = _build_audit_style_context(run_dir, blob, config)
    html = template.render(**ctx)
    path = run_dir / "cost_report.html"
    path.write_text(html)
    return path


@app.command()
def report(
    run: Path = typer.Option(..., "--run", help="Path to output/{timestamp}/ folder"),
    config: Path = typer.Option(
        Path("config/site.yaml"), "--config", help="site.yaml for branding"
    ),
) -> None:
    """Print + write a text/JSON/HTML cost breakdown for one audit run."""
    text, blob = build_report(run)
    typer.echo(text)
    txt_path = run / "cost_report.txt"
    json_path = run / "cost_report.json"
    txt_path.write_text(text)
    with json_path.open("w") as f:
        json.dump(blob, f, indent=2)

    site = SiteConfig.model_validate(yaml.safe_load(config.open()))
    html_path = write_html_report(run, blob, site)

    typer.echo(f"Wrote {txt_path}")
    typer.echo(f"Wrote {json_path}")
    typer.echo(f"Wrote {html_path}")


if __name__ == "__main__":
    app()
