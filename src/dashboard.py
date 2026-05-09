"""monitor-dashboard: logged-in product surface.

Users sign in with a Supabase magic link, register the brands + competitors
they want to track, kick off audits manually, and see headline metrics
trending over time.

Routes are mounted under /dashboard/ so they live alongside the existing
public marketing site (monitoraeo.com) without colliding."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime
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

# Default engines for a monitoring run. Cheapest single engine first; users
# can add more by editing the brand row.
DEFAULT_ENGINES = ["Google AI Overviews"]


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
    return _render("brand_list.html.j2", user=user, summaries=summaries)


@router.get("/brands/new", response_class=HTMLResponse)
def brand_new(request: Request):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return _render("brand_form.html.j2", user=user, brand=None)


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
):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
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
# Audit execution (background thread)
# ---------------------------------------------------------------------------

def _generic_brand_queries(brand_name: str) -> list:
    """Eight buyer-facing questions templated from the brand name. Mirrors the
    free-preview generator so monitoring uses the same shape of queries the
    public audit does, without a customer-supplied query CSV."""
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
            "engine_labels": list(brand.engines or DEFAULT_ENGINES),
            "country": brand.locale_country,
            "language": brand.locale_language,
        }

    try:
        # The existing pipeline expects a SiteConfig + a list of engine
        # adapters. Build both from the brand snapshot.
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
            # OpenRouter engines need explicit model IDs which we don't store
            # per-brand yet. v1 monitoring uses Google AI Overviews only;
            # extend by reading model IDs from a brand-level config later.
            engines=EnginesConfig(
                openrouter=[],
                apify=[ApifyEngineConfig(label="Google AI Overviews")],
            ),
        )
        engine_objs = [
            ApifyEngine(
                label="Google AI Overviews",
                country_code=brand_snapshot["country"],
                language_code=brand_snapshot["language"],
            )
        ]
        queries = _generic_brand_queries(brand_snapshot["name"])

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
        write_html(rows, site, run_dir, tier="full")

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
