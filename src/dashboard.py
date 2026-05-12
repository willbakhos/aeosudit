"""monitor-dashboard: logged-in product surface.

Users sign in with a Supabase magic link, register the brands + competitors
they want to track, see monthly cron-scheduled audits trended over time,
and browse past reports.

Routes are mounted under /dashboard/ so they live alongside the existing
public marketing site (monitoraeo.com) without colliding."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import select

from src.auth import (
    clear_session_cookie,
    current_user,
    send_magic_link,
    set_session_cookie,
    supabase_configured,
)
from src.db import AuditRunRecord, TrackedBrand, get_session


router = APIRouter(prefix="/dashboard")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(("html", "htm", "xml", "j2")),
)

# Default engines for a brand with no monitoring tier (free dashboard usage).
# Subscribers' tier (two_engine_monthly / full_monthly) overrides this — see
# _engines_for_brand below.
DEFAULT_ENGINES = ["Google AI Overviews"]
CHATGPT_LABEL = "ChatGPT"

# Subscribers get N runs per calendar month (cron + manual combined).
MONTHLY_RUN_QUOTA = 2


def _engines_for_brand(brand: TrackedBrand) -> set[str] | None:
    """Resolve which engines should run for a brand based on its tier.
    Returns None to mean "no filter / all configured engines".

    two_engine_monthly -> {Google AI Overviews, ChatGPT}
    full_monthly       -> all engines
    no tier            -> Google AI Overviews only (free dashboard view)
    """
    if brand.tier == "full_monthly":
        return None  # no filter — all configured engines
    if brand.tier == "two_engine_monthly":
        return {"Google AI Overviews", CHATGPT_LABEL}
    # Free dashboard usage: just Google AI Overviews
    return {"Google AI Overviews"}


def _compute_next_scheduled_run(now: datetime | None = None) -> datetime:
    """Return the 1st of next month at 06:00 UTC. The cron worker will pick
    these up in batches when their next_scheduled_run <= now()."""
    now = now or datetime.utcnow()
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, 6, 0, 0)


def _reset_monthly_counter_if_needed(brand: TrackedBrand) -> None:
    """Reset runs_this_month at the start of each calendar month so the quota
    refreshes. Mutates the brand in place; caller is responsible for committing."""
    now = datetime.utcnow()
    anchor = brand.runs_month_anchor
    if anchor is None or (anchor.year, anchor.month) != (now.year, now.month):
        brand.runs_this_month = 0
        brand.runs_month_anchor = now


def _render(name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("user", None)
    return HTMLResponse(_jinja.get_template(f"dashboard/{name}").render(**ctx))


def _require_user(request: Request) -> dict | RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)
    return user


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(sent: int = 0, error: str = "") -> HTMLResponse:
    return _render(
        "login.html.j2",
        sent=bool(sent),
        error=error or None,
        configured=supabase_configured(),
    )


@router.post("/login")
def login_submit(request: Request, email: str = Form(...)) -> RedirectResponse:
    if not supabase_configured():
        return RedirectResponse(
            "/dashboard/login?error=supabase_not_configured", status_code=303
        )
    redirect_url = str(request.url_for("auth_callback"))
    try:
        send_magic_link(email.strip(), redirect_url)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/dashboard/login?error={type(exc).__name__}", status_code=303
        )
    return RedirectResponse("/dashboard/login?sent=1", status_code=303)


@router.get("/auth/callback", name="auth_callback", response_class=HTMLResponse)
def auth_callback() -> HTMLResponse:
    """Supabase puts tokens in the URL fragment (#access_token=…). Fragments
    don't reach the server, so a tiny JS shim forwards the access_token to
    /dashboard/auth/exchange, which sets the HttpOnly cookie."""
    return HTMLResponse(
        """<!doctype html><meta charset="utf-8">
<title>Signing you in…</title>
<style>body{font-family:-apple-system,Inter,system-ui,sans-serif;
padding:60px;text-align:center;color:#0f172a;}</style>
<p>Signing you in…</p>
<script>
(async () => {
  const h = new URLSearchParams(window.location.hash.slice(1));
  const access = h.get("access_token");
  if (!access) {
    document.body.innerHTML = '<p>Missing access token. <a href="/dashboard/login">Try again</a>.</p>';
    return;
  }
  const r = await fetch("/dashboard/auth/exchange", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({access_token: access}),
    credentials: "same-origin",
  });
  if (r.ok) window.location.replace("/dashboard");
  else document.body.innerHTML = '<p>Login failed. <a href="/dashboard/login">Try again</a>.</p>';
})();
</script>"""
    )


@router.post("/auth/exchange")
async def auth_exchange(request: Request) -> JSONResponse:
    body = await request.json()
    access = (body or {}).get("access_token", "").strip()
    if not access:
        raise HTTPException(400, "missing access_token")
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, access)
    return resp


@router.post("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse("/dashboard/login", status_code=303)
    clear_session_cookie(resp)
    return resp


# ---------------------------------------------------------------------------
# Brand list + CRUD
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def index(request: Request):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    with get_session() as s:
        brands = list(
            s.exec(
                select(TrackedBrand)
                .where(TrackedBrand.user_id == UUID(user["id"]))
                .order_by(TrackedBrand.created_at.desc())
            )
        )
        summaries = []
        for b in brands:
            latest = s.exec(
                select(AuditRunRecord)
                .where(AuditRunRecord.brand_id == b.id)
                .order_by(AuditRunRecord.started_at.desc())
            ).first()
            summaries.append({"brand": b, "latest": latest})
    return _render("brand_list.html.j2", user=user, summaries=summaries, active_tab="brands")


@router.get("/brands/new", response_class=HTMLResponse)
def brand_new(request: Request):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return _render("brand_form.html.j2", user=user, brand=None, active_tab="brands")


def _parse_competitors(raw: str) -> list[dict[str, str]]:
    """Accept one competitor per line: 'Name' or 'Name | domain.com'."""
    out: list[dict[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, domain = [p.strip() for p in line.split("|", 1)]
        else:
            name, domain = line, ""
        out.append({"name": name, "domain": domain})
    return out


@router.post("/brands")
def brand_create(
    request: Request,
    name: str = Form(...),
    domain: str = Form(...),
    aliases: str = Form(""),
    competitors: str = Form(""),
    ground_truth: str = Form(""),
    locale_country: str = Form("US"),
    locale_language: str = Form("en"),
    tier: str = Form(""),
):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    valid_tiers = {"", "two_engine_monthly", "full_monthly"}
    tier_clean = tier.strip() if tier.strip() in valid_tiers else ""
    brand = TrackedBrand(
        user_id=UUID(user["id"]),
        name=name.strip(),
        domain=domain.strip(),
        aliases=[a.strip() for a in aliases.split(",") if a.strip()],
        competitors=_parse_competitors(competitors),
        ground_truth=[g.strip() for g in ground_truth.splitlines() if g.strip()],
        engines=list(DEFAULT_ENGINES),
        locale_country=locale_country.strip() or "US",
        locale_language=locale_language.strip() or "en",
        tier=tier_clean,
        next_scheduled_run=_compute_next_scheduled_run() if tier_clean else None,
    )
    with get_session() as s:
        s.add(brand)
        s.commit()
        s.refresh(brand)
        bid = brand.id
    return RedirectResponse(f"/dashboard/brands/{bid}", status_code=303)


@router.get("/brands/{brand_id}", response_class=HTMLResponse)
def brand_detail(request: Request, brand_id: str):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or str(brand.user_id) != user["id"]:
            raise HTTPException(404, "Brand not found")
        runs = list(
            s.exec(
                select(AuditRunRecord)
                .where(AuditRunRecord.brand_id == brand.id)
                .order_by(AuditRunRecord.started_at.asc())
            )
        )

    # Build a JSON-serialisable trend payload for the inline Chart.js block.
    trend = {
        "labels": [r.started_at.strftime("%Y-%m-%d %H:%M") for r in runs],
        "visibility": [r.visibility_rate for r in runs],
        "citation": [r.citation_rate for r in runs],
    }

    # Aggregate share-of-voice across the latest complete run.
    latest_complete = next(
        (r for r in reversed(runs) if r.status == "complete"), None
    )

    return _render(
        "brand_detail.html.j2",
        user=user,
        brand=brand,
        runs=list(reversed(runs)),  # newest first in the table
        trend_json=json.dumps(trend),
        latest=latest_complete,
        active_tab="brands",
    )


@router.post("/brands/{brand_id}/runs")
def brand_run(request: Request, brand_id: str):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or str(brand.user_id) != user["id"]:
            raise HTTPException(404, "Brand not found")
        from src.server import _make_run_id
        run_rec = AuditRunRecord(
            brand_id=brand.id,
            user_id=brand.user_id,
            run_id=_make_run_id(brand.name),
            status="running",
        )
        s.add(run_rec)
        s.commit()
        s.refresh(run_rec)
        run_record_id = str(run_rec.id)

    threading.Thread(
        target=_run_audit_for_brand,
        args=(str(bid), run_record_id),
        daemon=True,
    ).start()
    return RedirectResponse(f"/dashboard/brands/{brand_id}", status_code=303)


# ---------------------------------------------------------------------------
# Monitored queries — edit the buyer questions the cron audits each cycle
# ---------------------------------------------------------------------------

def _tier_query_limit(tier: str) -> int:
    """Look up the monitored_query_limit for a brand's tier. Imports lazily
    to avoid a circular import with src.server at module load."""
    from src.server import TIER_PLANS
    return int(TIER_PLANS.get(tier, {}).get("monitored_query_limit", 0))


def _load_owned_brand(request: Request, brand_id: str) -> tuple[dict, TrackedBrand]:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        raise HTTPException(401, "Sign in required")
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or str(brand.user_id) != user["id"]:
            raise HTTPException(404, "Brand not found")
        # Detach by expunging — caller works from in-memory state.
        s.expunge(brand)
    return user, brand


@router.get("/brands/{brand_id}/queries", response_class=HTMLResponse)
def brand_queries_edit(request: Request, brand_id: str, saved: int = 0, error: str = ""):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or str(brand.user_id) != user["id"]:
            raise HTTPException(404, "Brand not found")
        # Effective list = stored if any, else the templated seed
        if brand.monitored_queries:
            current = [q for q in brand.monitored_queries if q and q.strip()]
        else:
            current = [q.query for q in _generic_brand_queries(brand.name)]
        limit = _tier_query_limit(brand.tier)
        s.expunge(brand)
    return _render(
        "brand_queries.html.j2",
        user=user,
        brand=brand,
        queries_text="\n".join(current),
        query_count=len(current),
        limit=limit,
        saved=bool(saved),
        error=error or None,
        active_tab="monitoring",
    )


@router.post("/brands/{brand_id}/queries")
def brand_queries_save(
    request: Request,
    brand_id: str,
    queries: str = Form(""),
):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    # Parse + de-duplicate while preserving order
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in (queries or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(line)

    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or str(brand.user_id) != user["id"]:
            raise HTTPException(404, "Brand not found")
        limit = _tier_query_limit(brand.tier)
        if limit <= 0:
            return RedirectResponse(
                f"/dashboard/brands/{brand_id}/queries?error=needs_monitoring_tier",
                status_code=303,
            )
        if len(cleaned) > limit:
            return RedirectResponse(
                f"/dashboard/brands/{brand_id}/queries?error=over_limit",
                status_code=303,
            )
        brand.monitored_queries = cleaned
        brand.updated_at = datetime.utcnow()
        s.add(brand)
        s.commit()
    return RedirectResponse(
        f"/dashboard/brands/{brand_id}/queries?saved=1", status_code=303
    )


# ---------------------------------------------------------------------------
# Reports tab — every audit run across all brands, newest first
# ---------------------------------------------------------------------------

@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    with get_session() as s:
        brands = list(
            s.exec(
                select(TrackedBrand).where(TrackedBrand.user_id == UUID(user["id"]))
            )
        )
        brand_by_id = {b.id: b for b in brands}
        runs = list(
            s.exec(
                select(AuditRunRecord)
                .where(AuditRunRecord.user_id == UUID(user["id"]))
                .order_by(AuditRunRecord.started_at.desc())
            )
        )
    return _render(
        "reports.html.j2",
        user=user,
        runs=runs,
        brand_by_id=brand_by_id,
        active_tab="reports",
    )


# ---------------------------------------------------------------------------
# Monitoring tab — cron status, schedule, recent metric progression
# ---------------------------------------------------------------------------

@router.get("/monitoring", response_class=HTMLResponse)
def monitoring(request: Request):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    with get_session() as s:
        brands = list(
            s.exec(
                select(TrackedBrand)
                .where(TrackedBrand.user_id == UUID(user["id"]))
                .order_by(TrackedBrand.created_at.desc())
            )
        )
        # Last 6 complete runs per brand for the trend strip
        rows = []
        for b in brands:
            runs = list(
                s.exec(
                    select(AuditRunRecord)
                    .where(AuditRunRecord.brand_id == b.id)
                    .where(AuditRunRecord.status == "complete")
                    .order_by(AuditRunRecord.started_at.desc())
                )
            )[:6]
            runs.reverse()  # oldest -> newest left to right
            rows.append({
                "brand": b,
                "trend_runs": runs,
                "next_scheduled": b.next_scheduled_run or _compute_next_scheduled_run(),
                "monthly_quota": MONTHLY_RUN_QUOTA,
                "runs_left": max(0, MONTHLY_RUN_QUOTA - (b.runs_this_month or 0)),
            })
    return _render("monitoring.html.j2", user=user, rows=rows, active_tab="monitoring")


# ---------------------------------------------------------------------------
# Settings — profile / billing / team
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_root(request: Request):
    return _settings_page(request, "profile")


@router.get("/settings/profile", response_class=HTMLResponse)
def settings_profile(request: Request):
    return _settings_page(request, "profile")


@router.get("/settings/billing", response_class=HTMLResponse)
def settings_billing(request: Request):
    return _settings_page(request, "billing")


@router.get("/settings/team", response_class=HTMLResponse)
def settings_team(request: Request):
    return _settings_page(request, "team")


def _settings_page(request: Request, sub: str) -> HTMLResponse | RedirectResponse:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    with get_session() as s:
        brand_count = len(list(
            s.exec(select(TrackedBrand).where(TrackedBrand.user_id == UUID(user["id"])))
        ))
    return _render(
        "settings.html.j2",
        user=user,
        sub=sub,
        brand_count=brand_count,
        active_tab="settings",
    )


# ---------------------------------------------------------------------------
# Audit execution (background thread)
# ---------------------------------------------------------------------------

def _generic_brand_queries(brand_name: str) -> list:
    """Eight buyer-facing questions templated from the brand name. Used as the
    lazy seed when a brand has no `monitored_queries` set yet — both for the
    edit-page pre-fill and for the audit-time fallback so a free brand can
    still run a one-shot audit without anyone ever curating a query list."""
    from src.models import Query
    return [
        Query(query=f"What is {brand_name}?", type="brand"),
        Query(query=f"Is {brand_name} legitimate?", type="brand"),
        Query(query=f"{brand_name} reviews", type="brand"),
        Query(query=f"Best alternatives to {brand_name}", type="comparison"),
        Query(query=f"{brand_name} vs competitors", type="comparison"),
        Query(query=f"Should I use {brand_name}?", type="brand"),
        Query(query=f"How does {brand_name} work?", type="brand"),
        Query(query=f"{brand_name} pricing", type="brand"),
    ]


def _classify_query(text: str) -> str:
    """Best-effort type tag for a user-entered query. We use 'comparison' for
    anything that smells like a vs / alternative / best-of question, otherwise
    'brand'. The Query.type field is informational (drives heatmap grouping),
    so getting it loosely right is enough."""
    t = text.lower()
    if any(kw in t for kw in (" vs ", "alternatives", "alternative", "best ", "compare", "comparison", "instead of")):
        return "comparison"
    return "brand"


def _resolve_brand_queries(brand: TrackedBrand) -> list:
    """Return the list of Query objects to run for this brand. Honours the
    user's curated `monitored_queries` when present; otherwise falls back to
    the templated 8 so first-run / free-tier brands still get an audit."""
    from src.models import Query
    texts = [q.strip() for q in (brand.monitored_queries or []) if q and q.strip()]
    if not texts:
        return _generic_brand_queries(brand.name)
    return [Query(query=t, type=_classify_query(t)) for t in texts]


def _run_audit_for_brand(brand_id: str, run_record_id: str) -> None:
    """Background worker. Loads the brand, runs the existing audit pipeline,
    persists headline metrics back to the AuditRunRecord row."""
    # Imports inside the function so importing src.dashboard at server startup
    # doesn't pay the audit-pipeline import cost until a run actually fires.
    from src.engines.apify import ApifyEngine
    from src.engines.openrouter import OpenRouterEngine
    from src.models import (
        ApifyEngineConfig,
        BrandConfig,
        EnginesConfig,
        LLMScore,
        LocaleConfig,
        OpenRouterEngineConfig,
        ScoredRow,
        SiteConfig,
    )
    from src.report import write_csv, write_html
    from src.runner import run_audit
    from src.scorer import score_response

    output_root = Path(os.environ.get("OUTPUT_ROOT", "output"))

    with get_session() as s:
        brand = s.get(TrackedBrand, UUID(brand_id))
        run_rec = s.get(AuditRunRecord, UUID(run_record_id))
        if not brand or not run_rec:
            return
        run_id = run_rec.run_id
        engine_labels = _engines_for_brand(brand)
        # Resolve the query list while the brand row is still attached to the
        # session, then detach by stashing plain strings in the snapshot.
        resolved_queries = _resolve_brand_queries(brand)
        brand_snapshot = {
            "name": brand.name,
            "domain": brand.domain,
            "aliases": list(brand.aliases or []),
            "competitors": [
                c.get("name", "")
                for c in (brand.competitors or [])
                if c.get("name")
            ],
            "ground_truth": list(brand.ground_truth or []),
            "engine_labels": engine_labels,
            "country": brand.locale_country,
            "language": brand.locale_language,
            "tier": brand.tier or "",
        }

    try:
        # OpenRouter engine configs — only the labels the tier covers.
        # ChatGPT = gpt-5-mini, Claude = claude-haiku-4.5, Perplexity = sonar,
        # Gemini = google/gemini-2.5-flash. Match config/site.yaml.
        openrouter_models = {
            "ChatGPT": "openai/gpt-5-mini",
            "Claude": "anthropic/claude-haiku-4.5",
            "Perplexity": "perplexity/sonar",
            "Gemini": "google/gemini-2.5-flash",
        }
        labels = brand_snapshot["engine_labels"]  # set or None (means "all")
        openrouter_configs = [
            OpenRouterEngineConfig(label=lbl, model=mdl)
            for lbl, mdl in openrouter_models.items()
            if labels is None or lbl in labels
        ]
        apify_configs = (
            [ApifyEngineConfig(label="Google AI Overviews")]
            if labels is None or "Google AI Overviews" in labels
            else []
        )

        site = SiteConfig(
            brand=BrandConfig(
                name=brand_snapshot["name"],
                domain=brand_snapshot["domain"],
                aliases=brand_snapshot["aliases"],
            ),
            competitors=brand_snapshot["competitors"],
            ground_truth=brand_snapshot["ground_truth"],
            locale=LocaleConfig(
                country=brand_snapshot["country"],
                language=brand_snapshot["language"],
            ),
            engines=EnginesConfig(
                openrouter=openrouter_configs,
                apify=apify_configs,
            ),
        )
        engine_objs: list = []
        for cfg in openrouter_configs:
            engine_objs.append(OpenRouterEngine(model=cfg.model, label=cfg.label))
        for cfg in apify_configs:
            engine_objs.append(
                ApifyEngine(
                    label=cfg.label,
                    country_code=brand_snapshot["country"],
                    language_code=brand_snapshot["language"],
                )
            )
        queries = resolved_queries

        run_dir = output_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        responses = asyncio.run(run_audit(engine_objs, queries, run_dir))
        rows = [
            ScoredRow(
                response=r,
                deterministic=score_response(r, site),
                llm=LLMScore(),
            )
            for r in responses
        ]
        write_csv(rows, run_dir)
        # Monitoring runs always render the "full" report template so the
        # subscriber sees every metric the paid one-shot audit produces. If
        # their tier enables the action plan (full_monthly / two_engine_monthly),
        # generate it with Claude before rendering so the report includes it.
        from src.server import TIER_PLANS
        plan_cfg = TIER_PLANS.get(brand_snapshot["tier"], {})
        action_plan = None
        if plan_cfg.get("action_plan"):
            try:
                from src.action_plan import generate as generate_action_plan
                action_plan = generate_action_plan(rows, site)
            except Exception as exc:  # noqa: BLE001
                print(f"[audit] action_plan generation failed: {type(exc).__name__}: {exc}")
        write_html(rows, site, run_dir, tier="full", action_plan=action_plan)

        n = len(rows) or 1
        visibility = sum(1 for r in rows if r.deterministic.mentioned) / n
        citation = sum(1 for r in rows if r.deterministic.cited_as_source) / n
        sov: dict[str, int] = {}
        for r in rows:
            for c in r.deterministic.competitors_mentioned:
                sov[c] = sov.get(c, 0) + 1

        with get_session() as s:
            run_rec = s.get(AuditRunRecord, UUID(run_record_id))
            if run_rec:
                run_rec.finished_at = datetime.utcnow()
                run_rec.status = "complete"
                run_rec.queries_total = n
                run_rec.visibility_rate = visibility
                run_rec.citation_rate = citation
                run_rec.share_of_voice = sov
                s.add(run_rec)
            # Update brand scheduling state on successful runs.
            brand_row = s.get(TrackedBrand, UUID(brand_id))
            if brand_row:
                _reset_monthly_counter_if_needed(brand_row)
                brand_row.last_run_at = datetime.utcnow()
                brand_row.runs_this_month += 1
                # Recompute next scheduled run (1st of next month) if subscriber.
                if brand_row.tier:
                    brand_row.next_scheduled_run = _compute_next_scheduled_run()
                brand_row.updated_at = datetime.utcnow()
                s.add(brand_row)
            s.commit()
    except Exception as exc:  # noqa: BLE001
        with get_session() as s:
            run_rec = s.get(AuditRunRecord, UUID(run_record_id))
            if run_rec:
                run_rec.finished_at = datetime.utcnow()
                run_rec.status = "failed"
                run_rec.error = f"{type(exc).__name__}: {exc}"
                s.add(run_rec)
                s.commit()
