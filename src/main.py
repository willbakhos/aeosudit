"""CLI entrypoint."""
from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path

import typer
import yaml
from dotenv import load_dotenv

from src.action_plan import generate as generate_action_plan
from src.engines.apify import ApifyEngine
from src.engines.base import Engine
from src.engines.openrouter import OpenRouterEngine
from src.llm_scorer import score_all
from src.models import LLMScore, Query, ScoredRow, SiteConfig
from src.report import write_csv, write_html
from src.runner import run_audit
from src.scorer import score_response
from src.screenshot import capture as capture_screenshot

app = typer.Typer(add_completion=False, help="AEO Audit — query AI engines and score visibility.")


@app.callback()
def _root() -> None:
    """AEO Audit."""


def _load_config(path: Path) -> SiteConfig:
    with path.open() as f:
        data = yaml.safe_load(f)
    return SiteConfig.model_validate(data)


_TRUE_VALUES = {"true", "1", "yes", "y"}


def _load_queries(path: Path) -> list[Query]:
    queries: list[Query] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            q = (row.get("query") or "").strip()
            t = (row.get("type") or "general").strip()
            free = (row.get("free") or "").strip().lower() in _TRUE_VALUES
            spotlight = (row.get("spotlight") or "").strip().lower() in _TRUE_VALUES
            if q:
                queries.append(Query(query=q, type=t, free=free, spotlight=spotlight))
    return queries


def _build_engines(
    config: SiteConfig, only_labels: set[str] | None
) -> list[Engine]:
    engines: list[Engine] = []
    for cfg in config.engines.openrouter:
        if only_labels and cfg.label not in only_labels:
            continue
        engines.append(OpenRouterEngine(model=cfg.model, label=cfg.label))
    for cfg in config.engines.apify:
        if only_labels and cfg.label not in only_labels:
            continue
        engines.append(
            ApifyEngine(
                label=cfg.label,
                country_code=config.locale.country,
                language_code=config.locale.language,
            )
        )
    return engines


FREE_TIER_ENGINE = "Google AI Overviews"
VALID_TIERS = {"free", "spotlight", "full", "action_plan"}


@app.command()
def audit(
    config: Path = typer.Option(..., "--config", help="Path to site.yaml"),
    queries: Path = typer.Option(
        Path("config/queries.csv"), "--queries", help="Path to queries.csv"
    ),
    output_dir: Path = typer.Option(
        Path("output"), "--output-dir", help="Root output directory"
    ),
    skip_llm_scoring: bool = typer.Option(
        False, "--skip-llm-scoring", help="Skip the Haiku scoring pass"
    ),
    engines: str | None = typer.Option(
        None, "--engines", help="Comma-separated engine labels (overrides tier defaults)"
    ),
    tier: str = typer.Option(
        "full",
        "--tier",
        help=(
            "free = Google only, ~8 queries (lead magnet). "
            "spotlight = Google + 1 chosen engine, ~20 queries ($49). "
            "full = all engines × 40 queries + LLM scoring ($149). "
            "action_plan = full + Sonnet recommendations ($349)."
        ),
    ),
    spotlight_engine: str | None = typer.Option(
        None,
        "--spotlight-engine",
        help="Engine label to pair with Google for the Spotlight tier (Claude / ChatGPT / Perplexity / Gemini).",
    ),
) -> None:
    """Run an AEO audit and emit CSV + HTML report."""
    load_dotenv()

    tier = tier.lower().strip()
    if tier not in VALID_TIERS:
        typer.echo(
            f"Unknown --tier {tier!r}. Use one of: {', '.join(sorted(VALID_TIERS))}.",
            err=True,
        )
        raise typer.Exit(code=1)

    site = _load_config(config)
    query_list = _load_queries(queries)

    if tier == "free":
        query_list = [q for q in query_list if q.free]
        only_labels = {FREE_TIER_ENGINE}
        skip_llm_scoring = True
        if not query_list:
            typer.echo(
                "No queries flagged free=true in queries.csv. Mark ~8 with free=true.",
                err=True,
            )
            raise typer.Exit(code=1)
    elif tier == "spotlight":
        query_list = [q for q in query_list if q.spotlight or q.free]
        if not spotlight_engine:
            typer.echo(
                "Spotlight tier requires --spotlight-engine "
                "(e.g. --spotlight-engine ChatGPT).",
                err=True,
            )
            raise typer.Exit(code=1)
        valid_openrouter = {e.label for e in site.engines.openrouter}
        if spotlight_engine not in valid_openrouter:
            typer.echo(
                f"Unknown spotlight engine {spotlight_engine!r}. "
                f"Configured: {sorted(valid_openrouter)}",
                err=True,
            )
            raise typer.Exit(code=1)
        only_labels = {FREE_TIER_ENGINE, spotlight_engine}
        skip_llm_scoring = True
        if not query_list:
            typer.echo(
                "No queries flagged spotlight=true (or free=true) in queries.csv.",
                err=True,
            )
            raise typer.Exit(code=1)
    else:
        # full / action_plan: use all engines unless --engines overrides
        only_labels = (
            {s.strip() for s in engines.split(",") if s.strip()} if engines else None
        )

    engine_list = _build_engines(site, only_labels)

    if not engine_list:
        typer.echo("No engines configured (after filtering). Aborting.", err=True)
        raise typer.Exit(code=1)
    if not query_list:
        typer.echo("No queries loaded. Aborting.", err=True)
        raise typer.Exit(code=1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    total = len(query_list) * len(engine_list)
    typer.echo(
        f"Running {len(query_list)} queries × {len(engine_list)} engines "
        f"= {total} calls -> {run_dir}"
    )

    screenshot_path = capture_screenshot(site.brand.domain, run_dir)
    if screenshot_path:
        typer.echo(f"  Screenshot: {screenshot_path.name}")

    responses = asyncio.run(run_audit(engine_list, query_list, run_dir))

    if skip_llm_scoring:
        llm_scores: list[LLMScore | None] = [None] * len(responses)
    else:
        scored = asyncio.run(score_all(responses, site))
        llm_scores = list(scored)

    rows = [
        ScoredRow(
            response=r,
            deterministic=score_response(r, site),
            llm=llm,
        )
        for r, llm in zip(responses, llm_scores)
    ]

    action_plan: list[dict] | None = None
    if tier == "action_plan":
        typer.echo("Generating action plan via Sonnet…")
        action_plan = generate_action_plan(rows, site)
        if action_plan:
            (run_dir / "action_plan.json").write_text(
                json.dumps(action_plan, indent=2)
            )
            typer.echo(f"  Action plan: {len(action_plan)} recommendations")
        else:
            typer.echo("  Action plan: generation failed (continuing without)")

    csv_path = write_csv(rows, run_dir)
    html_path = write_html(
        rows,
        site,
        run_dir,
        tier=tier,
        screenshot=screenshot_path.name if screenshot_path else None,
        action_plan=action_plan,
    )

    errors = sum(1 for r in responses if r.error)
    typer.echo(f"Done. {errors} errors, {len(responses) - errors} ok.")
    typer.echo(f"  CSV:  {csv_path}")
    typer.echo(f"  HTML: {html_path}")


if __name__ == "__main__":
    app()
