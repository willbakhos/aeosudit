"""One-shot backfill: generate buyer_intro for every audited industry that
doesn't have one yet. Runs on demand (not part of the cron worker) so we
can populate all the industries at once instead of waiting up to 30 days
for each to hit its monthly refresh cycle.

Uses OpenRouter (GPT-4o via industry_narrative.BUYER_INTRO_MODEL).

Usage:
    python -m scripts.backfill_buyer_intros [--limit N] [--concurrency N] [--dry-run]

Cost: ~$0.005 per industry at current GPT-4o pricing.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlmodel import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import IndustryReport, IndustryBrand, get_session
from src.industry_narrative import generate_buyer_intro
from src.local_services import detect_local_services


def _pending_slugs() -> list[str]:
    """Fast: return slugs of audited industries that don't have a
    populated buyer_intro. One DB query even at 2000+ industries. The
    heavy per-slug data (brands, top sources) is loaded on demand in
    _build_job() only for slugs we're actually going to LLM-call."""
    with get_session() as s:
        reports = list(s.exec(
            select(IndustryReport).where(IndustryReport.last_full_refresh.is_not(None))
        ))
    pending = []
    for r in reports:
        if r.buyer_intro and r.buyer_intro.get("paragraphs"):
            continue
        pending.append(r.slug)
    return pending


def _build_job(slug: str) -> dict | None:
    """Load per-slug data needed to call generate_buyer_intro. Returns
    None if the industry has no brands (nothing to summarise)."""
    with get_session() as s:
        r = s.exec(select(IndustryReport).where(IndustryReport.slug == slug)).first()
        if r is None:
            return None
        brands = list(s.exec(
            select(IndustryBrand)
            .where(IndustryBrand.industry_slug == slug)
            .order_by(IndustryBrand.rank_in_industry.asc())
        ))
        if not brands:
            return None
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
            for b in brands
        ]
        source_counts: dict[str, int] = {}
        for b in brands:
            for src in (b.top_cited_sources or []):
                source_counts[src] = source_counts.get(src, 0) + 1
        top_sources = [s for s, _ in sorted(source_counts.items(), key=lambda kv: -kv[1])[:8]]
        is_local = bool(detect_local_services(
            slug=r.slug, name=r.name, parent_category=r.parent_category,
        ))
        return {
            "slug": r.slug, "name": r.name,
            "parent_category": r.parent_category or "",
            "brands": brand_dicts,
            "top_sources": top_sources,
            "is_local": is_local,
        }


def _run_one(slug: str) -> tuple[str, dict | None, str | None]:
    """Load job for slug + call LLM + return result. All heavy work runs
    inside the ThreadPoolExecutor worker so we can pipeline the DB fetches
    alongside the LLM calls."""
    try:
        job = _build_job(slug)
        if job is None:
            return slug, None, "no brands"
        result = generate_buyer_intro(
            industry_name=job["name"],
            parent_category=job["parent_category"],
            brands=job["brands"],
            top_cited_sources=job["top_sources"],
            is_local=job["is_local"],
        )
        return slug, result or None, None
    except Exception as exc:  # noqa: BLE001
        return slug, None, f"{type(exc).__name__}: {exc}"


def _persist(slug: str, intro: dict) -> None:
    with get_session() as s:
        r = s.exec(select(IndustryReport).where(IndustryReport.slug == slug)).first()
        if r is not None:
            r.buyer_intro = intro
            s.add(r)
            s.commit()


def _invalidate_cache(slug: str) -> None:
    """Drop the industry detail cache for this slug so the fresh intro
    shows up on the next request instead of waiting for the TTL. Best-
    effort; failure here shouldn't fail the backfill. Only reaches the
    in-process cache of THIS Python process, so it's a no-op when the
    backfill runs outside the web server (which is the normal case)."""
    try:
        from src.server import invalidate_ai_visibility_cache
        invalidate_ai_visibility_cache(slug)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N industries (default: all pending).")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="How many concurrent LLM calls to run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be backfilled, don't call the LLM.")
    ap.add_argument("--slug", type=str, default=None,
                    help="Only backfill this specific slug (useful for testing).")
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key and not args.dry_run:
        sys.exit("OPENROUTER_API_KEY not set — export it before running.")

    if args.slug:
        pending = [args.slug]
    else:
        pending = _pending_slugs()
        if args.limit:
            pending = pending[: args.limit]
    print(f"[backfill] {len(pending)} industries pending")

    if args.dry_run:
        for slug in pending[:20]:
            print(f"  DRY {slug}")
        if len(pending) > 20:
            print(f"  ... + {len(pending)-20} more")
        return

    started = time.monotonic()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for slug, intro, err in pool.map(_run_one, pending):
            if intro:
                _persist(slug, intro)
                _invalidate_cache(slug)
                ok += 1
                if ok % 25 == 0:
                    elapsed = time.monotonic() - started
                    rate = ok / max(elapsed, 1)
                    eta = (len(pending) - ok - fail) / max(rate, 1e-6)
                    print(f"[backfill] {ok+fail}/{len(pending)} done "
                          f"({ok} ok, {fail} fail), "
                          f"{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining")
            else:
                fail += 1
                print(f"[backfill] FAIL {slug}: {err or 'no result'}")
    elapsed = time.monotonic() - started
    print(f"[backfill] done in {elapsed:.0f}s: {ok} ok, {fail} fail out of {len(pending)}")


if __name__ == "__main__":
    main()
