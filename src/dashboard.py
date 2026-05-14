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

CLAIM_COOKIE = "monitor_claim"


@router.get("/login", response_class=HTMLResponse)
def login_page(sent: int = 0, error: str = "", claim: str = "") -> HTMLResponse:
    body = _render(
        "login.html.j2",
        sent=bool(sent),
        error=error or None,
        configured=supabase_configured(),
        claim=(claim or None),
    )
    if claim:
        # Survives the magic-link round-trip — read on /dashboard after sign-in.
        body.set_cookie(
            CLAIM_COOKIE, claim,
            max_age=3600, samesite="lax",
            secure=os.environ.get("COOKIE_SECURE", "1") == "1",
            httponly=False, path="/",
        )
    return body


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

def _claim_preview_run(run_id: str, user_id: str) -> str | None:
    """Hydrate a TrackedBrand + AuditRunRecord from a previously-run preview
    so the user can see it in their dashboard. Returns the brand_id (str) on
    success, None if the preview metadata is missing or the run was already
    claimed by someone else."""
    output_root = Path(os.environ.get("OUTPUT_ROOT", "output"))
    meta_path = output_root / run_id / "preview_meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    domain = (meta.get("domain") or "").strip()
    brand_name = (meta.get("brand_name") or "").strip()
    if not domain or not brand_name:
        return None
    with get_session() as s:
        # If this preview was already claimed (by anyone), do nothing.
        existing_run = s.exec(
            select(AuditRunRecord).where(AuditRunRecord.run_id == run_id)
        ).first()
        if existing_run:
            return str(existing_run.brand_id)
        # Re-use an existing brand (same user + domain) so claiming a second
        # preview for the same site doesn't duplicate the brand row.
        brand = s.exec(
            select(TrackedBrand)
            .where(TrackedBrand.user_id == UUID(user_id))
            .where(TrackedBrand.domain == domain)
        ).first()
        if brand is None:
            brand = TrackedBrand(
                user_id=UUID(user_id),
                name=brand_name,
                domain=domain,
                aliases=[domain],
                competitors=[],
                ground_truth=[],
                engines=[],
                locale_country=(meta.get("country") or "US").upper(),
                locale_language="en",
                tier="",
            )
            s.add(brand)
            s.commit()
            s.refresh(brand)
        run_rec = AuditRunRecord(
            brand_id=brand.id,
            user_id=UUID(user_id),
            run_id=run_id,
            status="complete",
            finished_at=datetime.utcnow(),
        )
        s.add(run_rec)
        s.commit()
        return str(brand.id)


@router.get("", response_class=HTMLResponse)
def index(request: Request):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    # If the user just signed in after running a free preview, claim the
    # report into their dashboard now (cookie set on /dashboard/login?claim=…).
    claim = request.cookies.get(CLAIM_COOKIE, "").strip()
    if claim:
        try:
            brand_id = _claim_preview_run(claim, user["id"])
        except Exception as exc:  # noqa: BLE001
            print(f"[claim] failed for {claim}: {type(exc).__name__}: {exc}")
            brand_id = None
        target = (
            f"/dashboard/brands/{brand_id}?claimed=1" if brand_id else "/dashboard"
        )
        resp = RedirectResponse(target, status_code=303)
        resp.delete_cookie(CLAIM_COOKIE, path="/")
        return resp

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


@router.get("/brands/{brand_id}/edit", response_class=HTMLResponse)
def brand_edit_page(request: Request, brand_id: str):
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
        s.expunge(brand)
    return _render("brand_form.html.j2", user=user, brand=brand, active_tab="brands")


@router.post("/brands/{brand_id}/edit")
def brand_update(
    request: Request,
    brand_id: str,
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
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    valid_tiers = {"", "two_engine_monthly", "full_monthly"}
    tier_clean = tier.strip() if tier.strip() in valid_tiers else ""
    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or str(brand.user_id) != user["id"]:
            raise HTTPException(404, "Brand not found")
        was_subscriber = bool(brand.tier)
        brand.name = name.strip()
        brand.domain = domain.strip()
        brand.aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        brand.competitors = _parse_competitors(competitors)
        brand.ground_truth = [g.strip() for g in ground_truth.splitlines() if g.strip()]
        brand.locale_country = (locale_country.strip() or "US").upper()
        brand.locale_language = locale_language.strip() or "en"
        brand.tier = tier_clean
        # Newly-subscribed brand → seed the next scheduled run; downgrades
        # clear it so the cron stops picking the brand up.
        if tier_clean and not was_subscriber:
            brand.next_scheduled_run = _compute_next_scheduled_run()
        elif not tier_clean:
            brand.next_scheduled_run = None
        brand.updated_at = datetime.utcnow()
        s.add(brand)
        s.commit()
    return RedirectResponse(f"/dashboard/brands/{brand_id}", status_code=303)


# Engine-set keys master accounts can pick from when starting a manual run.
ENGINE_SETS: dict[str, set[str] | None] = {
    "google_only": {"Google AI Overviews"},
    "two_engine": {"Google AI Overviews", CHATGPT_LABEL},
    "full": None,  # None means "no filter / all configured engines"
}


@router.post("/brands/{brand_id}/runs")
def brand_run(request: Request, brand_id: str, engine_set: str = Form("")):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    # Master accounts may override the tier-driven engine selection by passing
    # an engine_set in the form. Regular users always use their brand's tier.
    override: set[str] | None | str = "_no_override"
    if user.get("is_master") and engine_set in ENGINE_SETS:
        override = ENGINE_SETS[engine_set]
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

    thread_args = (str(bid), run_record_id)
    thread_kwargs: dict = {}
    if override != "_no_override":
        thread_kwargs["engines_override"] = override
    threading.Thread(
        target=_run_audit_for_brand,
        args=thread_args,
        kwargs=thread_kwargs,
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

# Section navigation for the embedded report view. Anchors target both the
# free-preview (report_free.html.j2) and full (report.html.j2) templates;
# any anchor not present in a given report just scrolls to the top of the
# iframe, which is a graceful no-op.
REPORT_SECTIONS: list[dict[str, str]] = [
    {"label": "Headline metrics", "anchor": "headline-metrics", "fallback": "visibility"},
    {"label": "Engine heatmap", "anchor": "engine-heatmap", "fallback": "engines"},
    {"label": "Action plan", "anchor": "action-plan", "fallback": "unlock"},
    {"label": "Top cited sources", "anchor": "top-cited-sources", "fallback": "sources"},
    {"label": "Competitor share-of-voice", "anchor": "competitor-sov", "fallback": "sources"},
    {"label": "Technical foundations", "anchor": "technical-foundations", "fallback": "foundations"},
    {"label": "Hallucination flags", "anchor": "hallucinations", "fallback": "evidence"},
    {"label": "Per-query drill-down", "anchor": "query-drilldown", "fallback": "evidence"},
]


@router.get("/reports/{run_id}", response_class=HTMLResponse)
def report_in_dashboard(request: Request, run_id: str):
    """Wrap the standalone report in the dashboard chrome with a left
    section nav. Existing /report/{run_id} URL keeps working for shared and
    emailed links — this just gives logged-in users a continuous experience."""
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    # Verify the run belongs to this user (or is a master account viewing
    # any run). Owners always pass; masters get cross-tenant access.
    with get_session() as s:
        run = s.exec(
            select(AuditRunRecord).where(AuditRunRecord.run_id == run_id)
        ).first()
        if not run:
            raise HTTPException(404, "Report not found")
        if str(run.user_id) != user["id"] and not user.get("is_master"):
            raise HTTPException(404, "Report not found")
        brand = s.get(TrackedBrand, run.brand_id)
    return _render(
        "report_view.html.j2",
        user=user,
        run=run,
        brand=brand,
        sections=REPORT_SECTIONS,
        active_tab="reports",
    )


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
        # Up to the most recent 12 complete runs per brand for the line chart.
        rows = []
        for b in brands:
            runs = list(
                s.exec(
                    select(AuditRunRecord)
                    .where(AuditRunRecord.brand_id == b.id)
                    .where(AuditRunRecord.status == "complete")
                    .order_by(AuditRunRecord.started_at.desc())
                )
            )[:12]
            runs.reverse()  # oldest -> newest left to right
            # Latest-run share-of-voice as a top-N bar series for the SoV tab.
            latest = runs[-1] if runs else None
            latest_sov = sorted(
                (latest.share_of_voice or {}).items() if latest else [],
                key=lambda kv: kv[1],
                reverse=True,
            )[:8]

            def _series(attr: str, *, scale: int = 100) -> list:
                """Build a numeric series with None gaps where the metric was
                never computed (e.g. older runs before LLM scoring rolled out)."""
                out = []
                for r in runs:
                    v = getattr(r, attr, None)
                    out.append(round(v * scale, 1) if v is not None else None)
                return out

            chart_payload = {
                "labels": [r.started_at.strftime("%b %d") for r in runs],
                "run_ids": [r.run_id for r in runs],
                "visibility": _series("visibility_rate"),
                "citation": _series("citation_rate"),
                "sentiment": _series("sentiment_avg"),
                "accuracy": _series("accuracy_avg"),
                "hallucinations": _series("hallucination_rate"),
                "sov_labels": [c for c, _ in latest_sov],
                "sov_counts": [n for _, n in latest_sov],
            }
            rows.append({
                "brand": b,
                "trend_runs": runs,
                "trend_json": json.dumps(chart_payload),
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
def settings_team(request: Request, sent: str = "", invited: str = ""):
    status_map = {"1": "invited", "send_failed": "send_failed", "invalid": "invalid_email"}
    return _settings_page(
        request,
        "team",
        team_status=status_map.get(sent),
        invited_email=invited or None,
    )


def _send_team_invite_email(
    *, owner_email: str, invitee_email: str, accept_url: str
) -> None:
    """Resend-backed invite email. Raises on failure so the route can flag it."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("REPORT_FROM_EMAIL", "").strip()
    if not api_key or not from_addr:
        raise RuntimeError("Resend not configured (RESEND_API_KEY / REPORT_FROM_EMAIL)")
    import resend
    resend.api_key = api_key
    body_html = f"""
      <p>{owner_email} has invited you to view their AI visibility audits on monitoraeo.</p>
      <p>Click below to sign in and gain read-only access to their brands and reports:</p>
      <p style="margin:18px 0;">
        <a href="{accept_url}"
           style="display:inline-block; padding:12px 22px; border-radius:999px;
                  background:#2563eb; color:white; text-decoration:none;
                  font-weight:700; font-family:Inter,sans-serif;">
          Accept invite
        </a>
      </p>
      <p style="color:#64748b; font-size:13px;">
        If you didn't expect this invite you can safely ignore this email.
      </p>
    """
    resend.Emails.send({
        "from": from_addr,
        "to": [invitee_email],
        "reply_to": owner_email,
        "subject": f"{owner_email} invited you to monitoraeo",
        "html": body_html,
    })


@router.post("/settings/team/invite")
def settings_team_invite(request: Request, email: str = Form(...)):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    invitee = (email or "").strip().lower()
    # Lightweight email shape check — just enough to reject obvious junk.
    if "@" not in invitee or "." not in invitee.split("@")[-1]:
        return RedirectResponse("/dashboard/settings/team?sent=invalid", status_code=303)
    from src.db import TeamInvite
    with get_session() as s:
        # Don't double-invite — re-use any existing pending invite.
        existing = s.exec(
            select(TeamInvite)
            .where(TeamInvite.owner_user_id == UUID(user["id"]))
            .where(TeamInvite.email == invitee)
            .where(TeamInvite.status == "pending")
        ).first()
        if existing is None:
            existing = TeamInvite(
                owner_user_id=UUID(user["id"]),
                email=invitee,
            )
            s.add(existing)
            s.commit()
            s.refresh(existing)
        token = existing.token
    base_url = os.environ.get("PUBLIC_BASE_URL", "https://monitoraeo.com").rstrip("/")
    accept_url = f"{base_url}/dashboard/team/accept?token={token}"
    try:
        _send_team_invite_email(
            owner_email=user["email"], invitee_email=invitee, accept_url=accept_url,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[team] invite email failed: {type(exc).__name__}: {exc}")
        return RedirectResponse(
            f"/dashboard/settings/team?sent=send_failed&invited={invitee}",
            status_code=303,
        )
    return RedirectResponse(
        f"/dashboard/settings/team?sent=1&invited={invitee}", status_code=303
    )


@router.post("/settings/team/invite/{invite_id}/revoke")
def settings_team_invite_revoke(request: Request, invite_id: str):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    from src.db import TeamInvite
    try:
        iid = UUID(invite_id)
    except ValueError:
        raise HTTPException(404, "Invite not found")
    with get_session() as s:
        inv = s.get(TeamInvite, iid)
        if not inv or str(inv.owner_user_id) != user["id"]:
            raise HTTPException(404, "Invite not found")
        inv.status = "revoked"
        s.add(inv)
        s.commit()
    return RedirectResponse("/dashboard/settings/team", status_code=303)


@router.get("/team/accept", response_class=HTMLResponse)
def team_accept(request: Request, token: str = ""):
    """Land here from the invite email. If signed in with the matching email,
    flip the invite to accepted; otherwise bounce through the magic-link login
    and come back. Cross-tenant brand visibility is a follow-up — for now
    accepting just records the relationship."""
    from src.db import TeamInvite
    if not token:
        raise HTTPException(404, "Invite not found")
    with get_session() as s:
        inv = s.exec(select(TeamInvite).where(TeamInvite.token == token)).first()
        if not inv or inv.status != "pending":
            return _render(
                "team_accept.html.j2",
                user=None,
                state="missing",
                invite=None,
                active_tab="",
            )
        user = current_user(request)
        if not user:
            return RedirectResponse(
                f"/dashboard/login?next=/dashboard/team/accept?token={token}",
                status_code=303,
            )
        if (user.get("email") or "").lower() != inv.email.lower():
            return _render(
                "team_accept.html.j2",
                user=user,
                state="email_mismatch",
                invite=inv,
                active_tab="",
            )
        inv.status = "accepted"
        inv.accepted_at = datetime.utcnow()
        s.add(inv)
        s.commit()
    return _render(
        "team_accept.html.j2",
        user=user,
        state="accepted",
        invite=inv,
        active_tab="",
    )


def _settings_page(
    request: Request,
    sub: str,
    *,
    team_status: str | None = None,
    invited_email: str | None = None,
) -> HTMLResponse | RedirectResponse:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    from src.db import TeamInvite
    from src.server import TIER_PLANS
    with get_session() as s:
        brands = list(
            s.exec(select(TrackedBrand).where(TrackedBrand.user_id == UUID(user["id"])))
        )
        brand_count = len(brands)
        # Subscription summary for the Billing tab.
        subscriptions = []
        for b in brands:
            if b.tier and b.tier in TIER_PLANS:
                plan = TIER_PLANS[b.tier]
                subscriptions.append({
                    "brand_name": b.name,
                    "tier_label": plan.get("label", b.tier),
                    "price": plan.get("price_usd", 0),
                    "next_run": b.next_scheduled_run,
                })
        # Pending invites the owner has sent for the Team tab.
        pending_invites = list(
            s.exec(
                select(TeamInvite)
                .where(TeamInvite.owner_user_id == UUID(user["id"]))
                .where(TeamInvite.status == "pending")
                .order_by(TeamInvite.created_at.desc())
            )
        )
    return _render(
        "settings.html.j2",
        user=user,
        sub=sub,
        brand_count=brand_count,
        subscriptions=subscriptions,
        stripe_portal_url=os.environ.get("STRIPE_PORTAL_URL", "").strip() or None,
        pending_invites=pending_invites,
        team_status=team_status,
        invited_email=invited_email,
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


def _run_audit_for_brand(
    brand_id: str,
    run_record_id: str,
    *,
    engines_override: set[str] | None | str = "_no_override",
) -> None:
    """Background worker. Loads the brand, runs the existing audit pipeline,
    persists headline metrics back to the AuditRunRecord row.
    `engines_override` lets master-account callers pick an engine set ad-hoc
    (None means "all engines", a set picks specific labels). The sentinel
    "_no_override" means fall back to the brand's tier."""
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
        # Honour an ad-hoc engine override (master-account manual runs);
        # otherwise fall back to the brand's subscription tier.
        if engines_override != "_no_override":
            engine_labels = engines_override  # type: ignore[assignment]
        else:
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
        # Capture the site screenshot in parallel with the audit so the report
        # hero has the same browser-frame visual the free preview uses.
        # Non-fatal — if the screenshot service is down we just render the
        # placeholder skeleton.
        try:
            from src.screenshot import capture as capture_screenshot
            screenshot_path = capture_screenshot(
                brand_snapshot["domain"], run_dir
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[audit] screenshot capture failed: {type(exc).__name__}: {exc}")
            screenshot_path = None
        responses = asyncio.run(run_audit(engine_objs, queries, run_dir))

        # Tier may enable LLM scoring (sentiment / accuracy / hallucination
        # flags). Run it now so the rows we score and persist have the full
        # picture — without this, the second-pass metrics stay None and the
        # trend chart can only plot visibility + citation.
        from src.server import TIER_PLANS
        plan_cfg = TIER_PLANS.get(brand_snapshot["tier"], {})
        llm_scores: list = []
        if plan_cfg.get("llm_scoring"):
            try:
                from src.llm_scorer import score_all
                llm_scores = list(asyncio.run(score_all(responses, site)))
            except Exception as exc:  # noqa: BLE001
                print(f"[audit] llm scoring failed: {type(exc).__name__}: {exc}")
                llm_scores = []
        rows = [
            ScoredRow(
                response=r,
                deterministic=score_response(r, site),
                llm=(llm_scores[i] if i < len(llm_scores) and llm_scores[i] else LLMScore()),
            )
            for i, r in enumerate(responses)
        ]
        write_csv(rows, run_dir)
        # Monitoring runs always render the "full" report template so the
        # subscriber sees every metric the paid one-shot audit produces. If
        # their tier enables the action plan, generate it with Claude before
        # rendering so the report includes it.
        action_plan = None
        if plan_cfg.get("action_plan"):
            try:
                from src.action_plan import generate as generate_action_plan
                action_plan = generate_action_plan(rows, site)
            except Exception as exc:  # noqa: BLE001
                print(f"[audit] action_plan generation failed: {type(exc).__name__}: {exc}")
        write_html(
            rows,
            site,
            run_dir,
            tier="full",
            action_plan=action_plan,
            screenshot=screenshot_path.name if screenshot_path else None,
        )

        # Aggregate the headline metrics for the dashboard trend chart.
        # Only count rows where the brand was actually mentioned for the
        # sentiment/accuracy averages — "not_mentioned" answers are noise
        # that would otherwise drag the rolling averages toward 0.
        n = len(rows) or 1
        visibility = sum(1 for r in rows if r.deterministic.mentioned) / n
        citation = sum(1 for r in rows if r.deterministic.cited_as_source) / n
        sov: dict[str, int] = {}
        for r in rows:
            for c in r.deterministic.competitors_mentioned:
                sov[c] = sov.get(c, 0) + 1

        sentiment_map = {"positive": 1.0, "neutral": 0.5, "negative": 0.0}
        accuracy_map = {"accurate": 1.0, "partial": 0.5, "inaccurate": 0.0}
        sent_vals = [
            sentiment_map[r.llm.sentiment]
            for r in rows
            if r.llm and r.llm.sentiment in sentiment_map
        ]
        acc_vals = [
            accuracy_map[r.llm.accuracy]
            for r in rows
            if r.llm and r.llm.accuracy in accuracy_map
        ]
        sentiment_avg = sum(sent_vals) / len(sent_vals) if sent_vals else None
        accuracy_avg = sum(acc_vals) / len(acc_vals) if acc_vals else None
        hallucination_rate = (
            sum(
                1
                for r in rows
                if r.llm
                and (r.llm.hallucination_flags or (r.llm.confidence and r.llm.confidence < 0.7))
            )
            / n
        )

        with get_session() as s:
            run_rec = s.get(AuditRunRecord, UUID(run_record_id))
            if run_rec:
                run_rec.finished_at = datetime.utcnow()
                run_rec.status = "complete"
                run_rec.queries_total = n
                run_rec.visibility_rate = visibility
                run_rec.citation_rate = citation
                run_rec.sentiment_avg = sentiment_avg
                run_rec.accuracy_avg = accuracy_avg
                run_rec.hallucination_rate = hallucination_rate
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
