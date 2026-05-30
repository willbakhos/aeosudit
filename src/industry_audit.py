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
    OR plain dicts — handles both so we can call this on either shape."""
    if not domain:
        return False
    target = domain.lower().lstrip("www.")
    for c in citations or []:
        d = (getattr(c, "domain", None) or (c.get("domain") if isinstance(c, dict) else None) or "")
        d = d.lower().lstrip("www.")
        if d == target or d.endswith("." + target):
            return True
        # Fallback: parse the URL if domain wasn't pre-extracted
        url = getattr(c, "url", None) or (c.get("url") if isinstance(c, dict) else None) or ""
        if url:
            netloc = urlparse(url).netloc.lower().lstrip("www.")
            if netloc == target or netloc.endswith("." + target):
                return True
    return False


def _extract_citation_domains(citations: list[Any]) -> list[str]:
    """Pull bare domain strings from a Citation list — used to build the
    'sources AI trusts in this category' pill row on the public page."""
    out: list[str] = []
    for c in citations or []:
        d = (getattr(c, "domain", None) or (c.get("domain") if isinstance(c, dict) else None) or "").lower().lstrip("www.")
        if not d:
            url = getattr(c, "url", None) or (c.get("url") if isinstance(c, dict) else None) or ""
            if url:
                d = urlparse(url).netloc.lower().lstrip("www.")
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
    from src.engines.apify import ApifyEngine
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
            # No brands to score — still advance the schedule so we don't hot-loop.
            report.last_full_refresh = datetime.utcnow()
            s.add(report)
            s.commit()
            summary["errors"].append("no brands in industry — nothing to score")
            return summary

    queries = _industry_queries(report.name)

    # Run all 8 queries against Apify Google AI Overviews. We do this OUTSIDE
    # the DB session so the connection isn't held open during 60+ seconds of
    # network I/O.
    try:
        engine = ApifyEngine(label=FREE_TIER_ENGINE, country_code="us", language_code="en")
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"engine init failed: {type(exc).__name__}: {exc}")
        return summary

    async def _gather():
        return await asyncio.gather(*[engine.query(q, "category") for q in queries])

    try:
        responses = asyncio.run(_gather())
        summary["queries_run"] = len(responses)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"query batch failed: {type(exc).__name__}: {exc}")
        return summary

    # Collect category-level source rollup once (used for the page's "top
    # cited sources in this category" row, persisted per-brand as the same
    # set since they all share the answer pool).
    category_sources: dict[str, int] = {}
    for r in responses:
        for d in _extract_citation_domains(getattr(r, "citations", None) or []):
            category_sources[d] = category_sources.get(d, 0) + 1
    top_category_sources = [d for d, _ in sorted(category_sources.items(), key=lambda kv: -kv[1])[:8]]

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
                brand.top_engine = "Google AI Overviews"
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
        # Advance the schedule.
        report.last_full_refresh = datetime.utcnow()
        from datetime import timedelta
        report.next_scheduled_refresh = datetime.utcnow() + timedelta(days=report.refresh_interval_days)
        s.add(report)
        s.commit()

    summary["elapsed_sec"] = round(time.monotonic() - started, 1)
    return summary
