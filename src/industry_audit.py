"""Industry visibility audit — refreshes one /ai-visibility/{slug} page.

Run-once-per-industry pattern:
  1. Generate 8 category-level buyer questions (not per-brand — they're shared)
  2. Query the Google AI Overviews engine once per question
  3. For each IndustryBrand row, count how many answers named the brand
     and how many cited its domain
  4. Compute visibility_pct, citation_pct, composite score per brand
  5. Re-rank, update timestamps

The "shared questions across all brands" design is the cost optimisation:
    8 queries × 50 industries × 12 months = 4,800 queries/year (~$14)
vs the naive per-brand-per-question version which would cost ~$340/year.

Failure mode: per-brand exceptions are logged into IndustryBrand.last_audit_error
but don't abort the rest of the refresh. A single bad domain shouldn't take
down the whole industry's monthly update.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import traceback
from datetime import datetime
from typing import Any
from urllib.parse import urlparse


# Composite visibility score weighting. Tuning these is the lever for what
# "winning" means: visibility-heavy rewards being named in the prose,
# citation-heavy rewards earning the clickable source link. Current 70/30
# matches the bias in the paid audit's headline score.
VISIBILITY_WEIGHT = 0.7
CITATION_WEIGHT = 0.3


def _build_industry_engine():
    """Picks the search engine for industry audits. Thin wrapper around the
    shared factory so industry audits use the exact same impl as the rest
    of the app — no separate INDUSTRY_SEARCH_ENGINE flag needed anymore,
    but the legacy env var is honoured by the factory for back-compat."""
    from src.engines.factory import build_default_engine
    return build_default_engine(country_code="us", language_code="en")


def _engine_display_name() -> str:
    """Human-facing engine name for template/JSON-LD/methodology copy.
    Delegates to the factory so it always matches the live impl."""
    from src.engines.factory import engine_display_name
    return engine_display_name()


def _advance_schedule_on_skip(session, report, days: int = 30) -> None:
    """Skip-path schedule advance for the no-brands branch — sets both
    last_full_refresh and next_scheduled_refresh so the cron doesn't keep
    selecting this row every tick."""
    from datetime import timedelta
    now = datetime.utcnow()
    report.last_full_refresh = now
    report.next_scheduled_refresh = now + timedelta(days=days)
    session.add(report)
    session.commit()


def _short_backoff_schedule(slug: str, days: int = 1) -> None:
    """Failure-path schedule advance: push next_scheduled_refresh out by
    `days` days so the cron stops hot-retrying the same stuck industry
    every 5 minutes. Short backoff so the row retries soon once whatever
    underlying issue (missing env var, vendor quota, etc.) is fixed.
    Opens its own session — we're called from points where the prior
    session may already be closed."""
    from datetime import timedelta
    from src.db import IndustryReport, get_session
    from sqlmodel import select as _select
    try:
        with get_session() as s:
            r = s.exec(_select(IndustryReport).where(IndustryReport.slug == slug)).first()
            if r:
                r.next_scheduled_refresh = datetime.utcnow() + timedelta(days=days)
                s.add(r)
                s.commit()
    except Exception as exc:  # noqa: BLE001
        # Last-line-of-defense: if we can't even advance the schedule, log
        # but don't raise — the caller is already in an error-return path.
        print(f"[industry_audit] short-backoff schedule advance failed for {slug}: {exc}")


def _industry_queries(industry_name: str) -> list[str]:
    """The 8 category-level buyer-intent queries we run per industry refresh.
    Mirrors the free-preview query shape so the methodology page can describe
    both in one paragraph. Industry name is the human-readable form
    (e.g. "CRM software", not the slug)."""
    ind = industry_name.strip()
    return [
        f"Best {ind}",
        f"Top {ind} 2026",
        f"Best {ind} for small business",
        f"Top {ind} alternatives",
        f"How do I choose the right {ind}?",
        f"{ind} pricing comparison",
        f"{ind} reviews",
        f"What is the best {ind} on the market?",
    ]


def _brand_in_text(brand: str, text: str) -> bool:
    """Case-insensitive substring match for the brand name inside an AI
    response. Mirrors src/scorer.py's deterministic check — kept here as a
    standalone helper so this module doesn't depend on the full scoring
    pipeline (which would pull in SiteConfig and friends)."""
    if not brand or not text:
        return False
    return brand.lower() in text.lower()


def _domain_in_citations(domain: str, citations: list[Any]) -> bool:
    """True if any citation URL is on the brand's domain (apex match,
    ignoring www prefix). Citations is a list of Citation pydantic models
    OR plain dicts — handles both so we can call this on either shape.

    Uses str.removeprefix, NOT str.lstrip (which takes a character SET and
    would corrupt any w-prefixed domain like webflow.com → ebflow.com)."""
    if not domain:
        return False
    target = domain.lower().removeprefix("www.")
    for c in citations or []:
        d = (getattr(c, "domain", None) or (c.get("domain") if isinstance(c, dict) else None) or "")
        d = d.lower().removeprefix("www.")
        if d == target or d.endswith("." + target):
            return True
        # Fallback: parse the URL if domain wasn't pre-extracted
        url = getattr(c, "url", None) or (c.get("url") if isinstance(c, dict) else None) or ""
        if url:
            netloc = urlparse(url).netloc.lower().removeprefix("www.")
            if netloc == target or netloc.endswith("." + target):
                return True
    return False


def _extract_citation_domains(citations: list[Any]) -> list[str]:
    """Pull bare domain strings from a Citation list — used to build the
    'sources AI trusts in this category' pill row on the public page.

    Uses removeprefix to strip www, NOT lstrip (which would corrupt domains
    starting with 'w' like wikipedia.org → ikipedia.org and write garbage
    to brand.top_cited_sources on the public page)."""
    out: list[str] = []
    for c in citations or []:
        d = (getattr(c, "domain", None) or (c.get("domain") if isinstance(c, dict) else None) or "").lower().removeprefix("www.")
        if not d:
            url = getattr(c, "url", None) or (c.get("url") if isinstance(c, dict) else None) or ""
            if url:
                d = urlparse(url).netloc.lower().removeprefix("www.")
        if d:
            out.append(d)
    return out


def refresh_industry(slug: str) -> dict[str, Any]:
    """Re-audit every brand in one industry. Returns a small summary dict
    (rows_updated, errors, elapsed_sec) for cron logging.

    Idempotent: safe to call repeatedly. Each call costs N queries where N is
    the length of _industry_queries() — currently 8."""
    # Inline imports so importing this module doesn't pull DB at startup.
    from sqlmodel import select
    from src.db import IndustryReport, IndustryBrand, get_session
    from src.main import FREE_TIER_ENGINE

    started = time.monotonic()
    summary: dict[str, Any] = {
        "slug": slug,
        "rows_updated": 0,
        "rows_failed": 0,
        "queries_run": 0,
        "errors": [],
    }

    with get_session() as s:
        report = s.exec(select(IndustryReport).where(IndustryReport.slug == slug)).first()
        if not report:
            summary["errors"].append(f"industry not found: {slug}")
            return summary
        brands = list(s.exec(select(IndustryBrand).where(IndustryBrand.industry_slug == slug)))
        if not brands:
            # No brands to score — still advance BOTH schedule fields so we
            # don't hot-loop (the cron WHERE selects nulls_first; advancing
            # only last_full_refresh would leave next_scheduled_refresh NULL
            # forever, the exact bug db.py:85-92 had to retroactively repair).
            _advance_schedule_on_skip(s, report, days=30)
            summary["errors"].append("no brands in industry — nothing to score")
            return summary

    queries = _industry_queries(report.name)

    # Run all 8 queries against the configured search engine. We do this
    # OUTSIDE the DB session so the connection isn't held open during 60+
    # seconds of network I/O.
    # INDUSTRY_SEARCH_ENGINE selects the backend:
    #   - "searchapi" → SearchApi.io google_ai_mode (much higher render rate
    #                    than AIO; preferred for niche/long-tail industries)
    #   - anything else (incl. unset) → ApifyEngine (Google AI Overviews;
    #                    historical default, kept for fallback / A/B)
    try:
        engine = _build_industry_engine()
    except Exception as exc:  # noqa: BLE001
        # Don't hot-loop on a missing env var or other init failure — push
        # next_scheduled_refresh out by 1 day so the same 3 industries don't
        # get retried every cron tick (5 min) until an operator fixes the env.
        summary["errors"].append(f"engine init failed: {type(exc).__name__}: {exc}")
        _short_backoff_schedule(slug, days=1)
        return summary

    async def _gather():
        # return_exceptions=True so a single coroutine raising doesn't cancel
        # all 8 sibling queries and trip the hot-loop. Each engine.query() is
        # already wrapped to return an EngineResponse with error set on
        # failure, but this defends against future bugs OR an exception path
        # outside that try (e.g. malformed response surviving _parse).
        return await asyncio.gather(
            *[engine.query(q, "category") for q in queries],
            return_exceptions=True,
        )

    try:
        gathered = asyncio.run(_gather())
        # Convert any raised exceptions into error-tagged EngineResponses so
        # the rest of the audit treats them uniformly with engine-level errors.
        from src.models import EngineResponse as _ER
        responses = []
        for q, r in zip(queries, gathered):
            if isinstance(r, Exception):
                responses.append(_ER(
                    engine_label=engine.label, query=q, query_type="category",
                    error=f"{type(r).__name__}: {r}",
                ))
            else:
                responses.append(r)
        summary["queries_run"] = len(responses)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"query batch failed: {type(exc).__name__}: {exc}")
        _short_backoff_schedule(slug, days=1)
        return summary

    # All-error guard: if most/every query errored (vendor quota, sustained
    # 5xx, network outage), DON'T overwrite real scores with zeros. The
    # scoring loop below would otherwise persist named=0, cited=0 for every
    # brand and tie-rank them — wiping the public page for ~30 days until
    # the next scheduled refresh. Bail out, log, and retry sooner.
    successful_responses = [r for r in responses if not r.error]
    err_count = len(responses) - len(successful_responses)
    summary["queries_errored"] = err_count
    if not successful_responses or len(successful_responses) < (len(responses) // 2):
        err_msgs = [r.error for r in responses if r.error][:3]
        summary["errors"].append(
            f"too many engine errors ({err_count}/{len(responses)}); "
            f"skipping write to preserve prior scores. sample errors: {err_msgs}"
        )
        _short_backoff_schedule(slug, days=1)
        return summary
    # From here on, score against `responses` — including the errored ones
    # contribute named=0/cited=0 for their slot, which is the correct
    # signal (we just refused to count THAT query, not all queries).

    # Collect category-level source rollup once (used for the page's "top
    # cited sources in this category" row, persisted per-brand as the same
    # set since they all share the answer pool).
    category_sources: dict[str, int] = {}
    for r in responses:
        for d in _extract_citation_domains(getattr(r, "citations", None) or []):
            category_sources[d] = category_sources.get(d, 0) + 1
    top_category_sources = [d for d, _ in sorted(category_sources.items(), key=lambda kv: -kv[1])[:8]]

    # Capture pre-refresh state (for the movers/shakers diff) and snapshot
    # every brand's current scores into IndustryBrandHistory BEFORE we
    # overwrite them. The narrative generator uses the previous scores to
    # spot biggest visibility shifts since last refresh.
    previous_scores: dict[str, dict[str, Any]] = {}  # keyed by brand_domain
    with get_session() as s:
        from src.db import IndustryBrandHistory
        for brand in s.exec(select(IndustryBrand).where(IndustryBrand.industry_slug == slug)):
            previous_scores[brand.brand_domain] = {
                "brand_name": brand.brand_name,
                "visibility_pct": brand.visibility_pct,
                "citation_pct": brand.citation_pct,
                "rank_in_industry": brand.rank_in_industry,
            }
            # Snapshot the existing scores (only if this brand has been
            # audited at least once — first refresh has nothing to snapshot).
            if brand.last_audited is not None:
                s.add(IndustryBrandHistory(
                    industry_slug=slug,
                    brand_name=brand.brand_name,
                    brand_domain=brand.brand_domain,
                    visibility_pct=brand.visibility_pct,
                    citation_pct=brand.citation_pct,
                    visibility_score=brand.visibility_score,
                    rank_in_industry=brand.rank_in_industry,
                    audited_at=brand.last_audited,
                ))
        s.commit()

    # Now score every brand against the response set.
    successful_brand_scores: list[tuple[int, float]] = []  # (brand_id, composite) for ranking
    with get_session() as s:
        for brand in s.exec(select(IndustryBrand).where(IndustryBrand.industry_slug == slug)):
            try:
                named = 0
                cited = 0
                for r in responses:
                    text = getattr(r, "response_text", "") or ""
                    citations = getattr(r, "citations", None) or []
                    if _brand_in_text(brand.brand_name, text):
                        named += 1
                    if _domain_in_citations(brand.brand_domain, citations):
                        cited += 1
                total = len(responses) or 1
                brand.visibility_pct = (named / total) * 100.0
                brand.citation_pct = (cited / total) * 100.0
                brand.visibility_score = (
                    brand.visibility_pct * VISIBILITY_WEIGHT
                    + brand.citation_pct * CITATION_WEIGHT
                )
                # Per-engine breakdown: only one engine right now (Google AI).
                # Schema'd as a dict so adding ChatGPT/Claude later doesn't
                # require migration.
                brand.visibility_per_engine = {
                    "google_ai": {
                        "visibility": brand.visibility_pct,
                        "citations": brand.citation_pct,
                    }
                }
                brand.top_engine = engine.label
                brand.top_cited_sources = top_category_sources
                brand.last_audited = datetime.utcnow()
                brand.last_audit_error = None
                s.add(brand)
                successful_brand_scores.append((brand.id, brand.visibility_score))
                summary["rows_updated"] += 1
            except Exception as exc:  # noqa: BLE001
                brand.last_audit_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                s.add(brand)
                summary["rows_failed"] += 1
                summary["errors"].append(f"{brand.brand_domain}: {type(exc).__name__}")
                traceback.print_exc()
        s.commit()

        # Re-rank by composite score (highest first). Done in a separate pass
        # so any per-brand exception above doesn't leave the ranking partial.
        ranked = sorted(successful_brand_scores, key=lambda kv: -kv[1])
        for rank, (brand_id, _score) in enumerate(ranked, 1):
            row = s.get(IndustryBrand, brand_id)
            if row:
                row.rank_in_industry = rank
                s.add(row)
        # Advance the schedule. `report` was loaded in an earlier session
        # block and is now detached — modifying its attributes then calling
        # s.add() can fail silently to persist the changes. Reload via
        # s.get() so SQLAlchemy tracks the writes as dirty in THIS session.
        from datetime import timedelta
        fresh_report = s.get(IndustryReport, slug)
        if fresh_report:
            interval = fresh_report.refresh_interval_days or 30
            fresh_report.last_full_refresh = datetime.utcnow()
            fresh_report.next_scheduled_refresh = datetime.utcnow() + timedelta(days=interval)
            # Empty-page guardrail: if NO brand scored any visibility or
            # citation, this ranking is empty and shouldn't be indexed by
            # Google. Reversible — next refresh that finds any score flips
            # it back to FALSE automatically.
            from sqlmodel import func as _func
            scored_row = s.exec(
                select(_func.count(IndustryBrand.id))
                .where(IndustryBrand.industry_slug == slug)
                .where((IndustryBrand.visibility_pct > 0) | (IndustryBrand.citation_pct > 0))
            ).first()
            scored_count = scored_row if isinstance(scored_row, int) else (scored_row[0] if scored_row else 0)
            was_noindex = bool(getattr(fresh_report, "noindex", False))
            fresh_report.noindex = (scored_count == 0)
            summary["scored_count"] = int(scored_count)
            summary["noindex"] = fresh_report.noindex
            if fresh_report.noindex and not was_noindex:
                print(f"[industry_audit] {slug}: marking NOINDEX (no brand scored)")
            elif was_noindex and not fresh_report.noindex:
                print(f"[industry_audit] {slug}: clearing NOINDEX ({scored_count} brands scored)")
            s.add(fresh_report)
        s.commit()

    # Generate the AI narrative + FAQs + per-brand insights using fresh
    # post-refresh data + the previous_scores snapshot to compute movers.
    # Failures here don't roll back the audit — we just leave the
    # narrative empty and the template falls back to the bare ranking.
    try:
        from src.industry_narrative import generate as generate_narrative
        with get_session() as s:
            fresh_brands = list(
                s.exec(
                    select(IndustryBrand)
                    .where(IndustryBrand.industry_slug == slug)
                    .order_by(IndustryBrand.rank_in_industry.asc())
                )
            )
            brand_dicts = [
                {
                    "brand_name": b.brand_name,
                    "brand_domain": b.brand_domain,
                    "rank_in_industry": b.rank_in_industry,
                    "visibility_pct": b.visibility_pct,
                    "citation_pct": b.citation_pct,
                    "visibility_score": b.visibility_score,
                    "top_engine": b.top_engine,
                }
                for b in fresh_brands
            ]
        movers = _compute_movers(brand_dicts, previous_scores)
        narrative = generate_narrative(
            industry_name=report.name,
            parent_category=report.parent_category or "",
            brands=brand_dicts,
            top_cited_sources=top_category_sources,
            movers=movers,
            engine_name=engine.label,
        )
        if narrative:
            # Cache movers alongside the narrative so the template can
            # render the Movers & Shakers section without recomputing.
            narrative["movers"] = movers
            with get_session() as s:
                fresh_report = s.exec(select(IndustryReport).where(IndustryReport.slug == slug)).first()
                if fresh_report:
                    fresh_report.narrative = narrative
                    s.add(fresh_report)
                    s.commit()
            summary["narrative_generated"] = True
        else:
            summary["narrative_generated"] = False
    except Exception as exc:  # noqa: BLE001
        print(f"[industry_audit] narrative generation failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        summary["narrative_error"] = f"{type(exc).__name__}: {exc}"

    summary["elapsed_sec"] = round(time.monotonic() - started, 1)

    # Drop the public index + this slug's detail cache so newly refreshed
    # rankings show up immediately instead of after the 60s TTL. Lazy
    # import to dodge startup circular (server -> cron_worker -> industry_audit).
    try:
        from src.server import invalidate_ai_visibility_cache
        invalidate_ai_visibility_cache(slug)
    except Exception:  # noqa: BLE001
        pass

    return summary


def _compute_movers(
    current_brands: list[dict[str, Any]],
    previous_scores: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Compare current vs previous brand scores. Returns top 3 risers and
    top 3 fallers by visibility_pct delta. Brands without a previous
    snapshot (first refresh) are excluded — they have no delta to compute.
    Sub-threshold movements (<5pp absolute delta) are filtered out so we
    don't show noise as a 'mover'."""
    deltas: list[dict[str, Any]] = []
    for b in current_brands:
        prev = previous_scores.get(b["brand_domain"])
        if not prev:
            continue
        delta_vis = b["visibility_pct"] - prev["visibility_pct"]
        delta_cit = b["citation_pct"] - prev["citation_pct"]
        delta_rank = prev["rank_in_industry"] - b["rank_in_industry"]  # positive = up
        if abs(delta_vis) < 5:
            continue
        deltas.append({
            "brand_name": b["brand_name"],
            "brand_domain": b["brand_domain"],
            "current_rank": b["rank_in_industry"],
            "previous_rank": prev["rank_in_industry"],
            "rank_change": delta_rank,
            "visibility_delta": round(delta_vis, 1),
            "citation_delta": round(delta_cit, 1),
            "current_visibility": round(b["visibility_pct"], 1),
            "previous_visibility": round(prev["visibility_pct"], 1),
        })
    risers = sorted([d for d in deltas if d["visibility_delta"] > 0],
                    key=lambda d: -d["visibility_delta"])[:3]
    fallers = sorted([d for d in deltas if d["visibility_delta"] < 0],
                     key=lambda d: d["visibility_delta"])[:3]
    return {"risers": risers, "fallers": fallers}
