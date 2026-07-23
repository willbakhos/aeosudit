"""CSV + single-file HTML report."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.models import ScoredRow, SiteConfig

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _row_dict(row: ScoredRow) -> dict[str, Any]:
    r = row.response
    d = row.deterministic
    llm = row.llm
    # Citations: full URL list from the engine response. Semicolon-joined so
    # Excel treats each column as a single cell (commas would split into
    # extra columns). citation_urls preserves order + duplicates so it maps
    # back to the answer prose position; citation_domains is deduped for
    # quick pivot on "which domains did the AI trust across this run".
    citation_urls = [c.url for c in r.citations if c.url]
    citation_titles = [c.title or "" for c in r.citations]
    seen_dom: set[str] = set()
    citation_domains: list[str] = []
    for c in r.citations:
        d_l = (c.domain or "").lower()
        if d_l and d_l not in seen_dom:
            seen_dom.add(d_l)
            citation_domains.append(c.domain)
    return {
        "query": r.query,
        "query_type": r.query_type,
        "engine": r.engine_label,
        "mentioned": d.mentioned,
        "mention_position": d.mention_position,
        "cited_as_source": d.cited_as_source,
        "citation_rank": d.citation_rank,
        "competitors_mentioned": ", ".join(d.competitors_mentioned),
        "competitors_cited": ", ".join(d.competitors_cited),
        "sentiment": llm.sentiment if llm else "",
        "accuracy": llm.accuracy if llm else "",
        "hallucination_flags": " | ".join(llm.hallucination_flags) if llm else "",
        "confidence": llm.confidence if llm else "",
        "response_text": r.response_text,
        "citation_count": len(citation_urls),
        "citation_urls": "; ".join(citation_urls),
        "citation_domains": "; ".join(citation_domains),
        "citation_titles": "; ".join(citation_titles),
        "latency_ms": r.latency_ms,
        "error": r.error or "",
    }


def write_csv(rows: list[ScoredRow], output_dir: Path) -> Path:
    df = pd.DataFrame([_row_dict(r) for r in rows])
    path = output_dir / "results.csv"
    df.to_csv(path, index=False)
    return path


def _aggregate(rows: list[ScoredRow], config: SiteConfig) -> dict[str, Any]:
    total = len(rows)
    valid = [r for r in rows if not r.response.error]
    visible = sum(1 for r in valid if r.deterministic.mentioned)
    cited = sum(1 for r in valid if r.deterministic.cited_as_source)
    competitor_counts_in_text: Counter[str] = Counter()
    competitor_counts_in_citations: Counter[str] = Counter()
    for r in valid:
        for c in r.deterministic.competitors_mentioned:
            competitor_counts_in_text[c] += 1
        for c in r.deterministic.competitors_cited:
            competitor_counts_in_citations[c] += 1

    own = config.brand.domain.lower()
    domain_counts: Counter[str] = Counter()
    for r in valid:
        for d in r.deterministic.all_cited_domains:
            d_norm = d.lower()
            d_norm = d_norm[4:] if d_norm.startswith("www.") else d_norm
            if d_norm == own or d_norm.endswith("." + own):
                continue
            domain_counts[d_norm] += 1

    # heatmap: query_type × engine -> visibility %
    by_cell: dict[tuple[str, str], list[bool]] = defaultdict(list)
    engines = sorted({r.response.engine_label for r in rows})
    types = sorted({r.response.query_type for r in rows})
    for r in valid:
        by_cell[(r.response.query_type, r.response.engine_label)].append(
            r.deterministic.mentioned
        )
    heatmap: list[dict[str, Any]] = []
    for t in types:
        cells: list[dict[str, Any]] = []
        for e in engines:
            vals = by_cell.get((t, e), [])
            pct = round(100 * sum(vals) / len(vals), 1) if vals else None
            cells.append({"engine": e, "pct": pct, "n": len(vals)})
        heatmap.append({"type": t, "cells": cells})

    avg_competitors = (
        round(sum(len(r.deterministic.competitors_cited) for r in valid) / len(valid), 2)
        if valid
        else 0.0
    )

    flagged = [
        r
        for r in rows
        if r.llm and (r.llm.hallucination_flags or r.llm.confidence < 0.7)
    ]

    by_query: dict[str, list[ScoredRow]] = defaultdict(list)
    for r in rows:
        by_query[r.response.query].append(r)

    return {
        "total": total,
        "visible_pct": round(100 * visible / len(valid), 1) if valid else 0.0,
        "cited_pct": round(100 * cited / len(valid), 1) if valid else 0.0,
        "avg_competitors_cited": avg_competitors,
        "competitor_text_counts": competitor_counts_in_text.most_common(),
        "competitor_citation_counts": competitor_counts_in_citations.most_common(),
        "top_domains": domain_counts.most_common(20),
        "heatmap": heatmap,
        "engines": engines,
        "types": types,
        "flagged": flagged,
        "by_query": dict(by_query),
        "errors": [r for r in rows if r.response.error],
    }


PREVIEW_TIERS = {"free", "spotlight"}


def write_html(
    rows: list[ScoredRow],
    config: SiteConfig,
    output_dir: Path,
    tier: str = "full",
    screenshot: str | None = None,
    action_plan: list[dict] | None = None,
    tech=None,  # TechAudit | None
    generated_at: datetime | None = None,
) -> Path:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(("html", "htm", "xml", "j2")),
    )
    template_name = (
        "report_free.html.j2" if tier in PREVIEW_TIERS else "report.html.j2"
    )
    template = env.get_template(template_name)
    when = generated_at or datetime.now(timezone.utc)
    ctx = {
        "config": config,
        "agg": _aggregate(rows, config),
        "tier": tier,
        "screenshot": screenshot,
        "action_plan": action_plan,
        "tech": tech,
        # ISO 8601 with Z so the JS countdown can `new Date(...)` it directly
        "generated_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # output_dir.name is the run_id — used by the in-report Save-this-report
        # email form to pass `claim` so /dashboard/login can hydrate the
        # brand + run record after sign-in.
        "run_id": output_dir.name,
    }
    html = template.render(**ctx)
    path = output_dir / "report.html"
    path.write_text(html)
    return path
