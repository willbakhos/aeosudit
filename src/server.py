"""FastAPI server: Stripe Checkout + webhook → triggers an audit run, generates
PDF, emails the customer.

Run locally:
    uvicorn src.server:app --reload --port 8000

Stripe webhook for local dev:
    stripe listen --forward-to localhost:8000/webhooks/stripe
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import string
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import stripe
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, EmailStr, Field

from src.models import (
    ApifyEngineConfig,
    BrandConfig,
    EnginesConfig,
    LocaleConfig,
    OpenRouterEngineConfig,
    Query,
)

from src.action_plan import generate as generate_action_plan
from src.dashboard import router as dashboard_router
from src.db import init_db
from src.delivery import send_report
from src.engines.apify import ApifyEngine
from src.engines.openrouter import OpenRouterEngine
from src.llm_scorer import extract_competitors, score_all
from src.main import FREE_TIER_ENGINE, _load_queries, VALID_TIERS
from src.models import LLMScore, ScoredRow, SiteConfig
from src.report import write_csv, write_html
from src.runner import run_audit
from src.scorer import score_response
from src.screenshot import capture as capture_screenshot
from src.tech_audit import run_for_domain_async as run_tech_audit_async

load_dotenv()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
DEFAULT_CONFIG_PATH = Path(os.environ.get("DEFAULT_SITE_CONFIG", "config/site.yaml"))
DEFAULT_QUERIES_PATH = Path(os.environ.get("DEFAULT_QUERIES_CSV", "config/queries.csv"))
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "output"))

# Single source of truth for paid tier behaviour. Adding/removing a tier
# means editing this dict + creating the matching Stripe product. Engine
# names match the labels in config/site.yaml (Google AI Overviews + ChatGPT
# / Claude / Perplexity / Gemini). "all" means no filter.
CHATGPT_LABEL = "ChatGPT"

TIER_PLANS: dict[str, dict[str, Any]] = {
    "two_engine": {
        "label": "Two Engine Audit ($29)",
        "price_usd": 29,
        "stripe_mode": "payment",
        "stripe_env": "STRIPE_PRICE_TWO_ENGINE",
        "engines": ["Google AI Overviews", CHATGPT_LABEL],
        "llm_scoring": True,
        "action_plan": True,
        "monitored_query_limit": 0,  # one-shot tier — n/a
    },
    "full_audit": {
        "label": "Full Audit ($79)",
        "price_usd": 79,
        "stripe_mode": "payment",
        "stripe_env": "STRIPE_PRICE_FULL_AUDIT",
        "engines": "all",
        "llm_scoring": True,
        "action_plan": True,
        "monitored_query_limit": 0,
    },
    "two_engine_monthly": {
        "label": "Two Engine Monitoring ($35/mo)",
        "price_usd": 35,
        "stripe_mode": "subscription",
        "stripe_env": "STRIPE_PRICE_TWO_ENGINE_MONTHLY",
        "engines": ["Google AI Overviews", CHATGPT_LABEL],
        "llm_scoring": True,
        "action_plan": True,
        "monitored_query_limit": 20,
    },
    "full_monthly": {
        "label": "Full Monitoring ($95/mo)",
        "price_usd": 95,
        "stripe_mode": "subscription",
        "stripe_env": "STRIPE_PRICE_FULL_MONTHLY",
        "engines": "all",
        "llm_scoring": True,
        "action_plan": True,
        "monitored_query_limit": 40,
    },
}

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(("html", "htm", "xml", "j2")),
)

# In-memory job tracker for free previews. Fine for single-process MVP;
# swap for Redis when you scale beyond one uvicorn worker.
PREVIEW_JOBS: dict[str, dict[str, Any]] = {}

# Paid orders waiting for the customer to enter their competitor list before
# the audit kicks off. Keyed by Stripe session_id. Same MVP storage caveat.
PENDING_ORDERS: dict[str, dict[str, Any]] = {}

# Live progress tracker for paid audits in flight. Keyed by email_lower so the
# dashboard can show 'your audit is running' without needing session_id.
# Each entry: {step, pct, brand, domain, started_at, status}.
# status flows: queued → running → complete | failed.
PAID_AUDIT_JOBS: dict[str, dict[str, Any]] = {}


def _publish_paid_status(email: str, step: str, pct: int, **extra) -> None:
    """Publish a stage update for a paid-audit-in-flight so the dashboard's
    'pending audit' banner can show real progress instead of a spinner."""
    if not email:
        return
    key = email.strip().lower()
    job = PAID_AUDIT_JOBS.get(key, {})
    job["step"] = step
    job["pct"] = pct
    job["status"] = "running"
    job.update(extra)
    PAID_AUDIT_JOBS[key] = job

_SLUG_ALPHABET = string.ascii_lowercase + string.digits


def _slugify(name: str, max_len: int = 40) -> str:
    """Lowercase, replace runs of non-alphanumerics with single hyphens, trim
    to max_len. Falls back to 'audit' for unusable inputs (empty / all symbols)."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:max_len].strip("-") or "audit"


def _make_run_id(brand_name: str) -> str:
    """5-char random prefix + slugified brand name, e.g. `xr2pk-jb-hi-fi`.
    The prefix collides only 1 in ~60M, enough to differentiate same-brand
    audits without a database lookup."""
    prefix = "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(5))
    return f"{prefix}-{_slugify(brand_name)}"

app = FastAPI(title="monitoraeo")
app.include_router(dashboard_router)

# Public assets (logos, team photos, OG images, anything you want hosted at a
# stable URL). Drop files into the repo's `static/` dir and reference them at
# https://www.monitoraeo.com/static/<filename>.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.on_event("startup")
def _init_dashboard_db() -> None:
    """Best-effort: create monitor-dashboard tables + start the cron worker.
    The public marketing + checkout flow does not depend on Postgres, so we
    swallow errors here — the dashboard surface will surface them at use."""
    if os.environ.get("DATABASE_URL", "").strip():
        try:
            init_db()
        except Exception as exc:  # noqa: BLE001
            print(f"[monitor-dashboard] init_db skipped: {exc}")
        # Start the monitoring cron worker. Idempotent + safe if disabled.
        try:
            from src.cron_worker import start as start_cron
            start_cron()
        except Exception as exc:  # noqa: BLE001
            print(f"[monitor-dashboard] cron worker not started: {exc}")


@app.get("/health")
def health() -> dict[str, bool]:
    """Liveness probe for Railway / uptime monitors."""
    return {"ok": True}


class CheckoutRequest(BaseModel):
    tier: str = Field(
        ..., description="two_engine | full_audit | two_engine_monthly | full_monthly"
    )
    brand_name: str
    domain: str
    email: EmailStr


SITE_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://monitoraeo.com").rstrip("/")


def _render(name: str, request: Request | None = None, **ctx: Any) -> HTMLResponse:
    """Render a template (auto-prefixes 'pages/' for static pages).
    Always injects base_url so the layout can build canonical/og URLs.
    When `request` is provided we also resolve the Supabase session and
    inject `user` so the public nav can show Login vs Dashboard."""
    if "/" not in name:
        name = f"pages/{name}"
    ctx.setdefault("base_url", SITE_BASE_URL)
    if "user" not in ctx:
        try:
            from src.auth import current_user
            ctx["user"] = current_user(request) if request is not None else None
        except Exception:  # noqa: BLE001
            ctx["user"] = None
    return HTMLResponse(_jinja.get_template(name).render(**ctx))


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    from src.auth import current_user
    return HTMLResponse(
        _jinja.get_template("landing.html.j2").render(
            base_url=SITE_BASE_URL,
            user=current_user(request),
        )
    )


# Pages eligible for the public sitemap (path, change-frequency, priority).
SITEMAP_PAGES: list[tuple[str, str, str]] = [
    ("/", "weekly", "1.0"),
    ("/pricing", "monthly", "0.9"),
    ("/what-is-aeo", "monthly", "0.8"),
    ("/what-is-geo", "monthly", "0.8"),
    ("/aeo-vs-seo", "monthly", "0.8"),
    ("/product/audit", "monthly", "0.8"),
    ("/product/monitoring", "monthly", "0.6"),
    ("/how-it-works", "monthly", "0.7"),
    ("/support", "monthly", "0.4"),
    ("/privacy", "yearly", "0.2"),
    ("/terms", "yearly", "0.2"),
]


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt() -> PlainTextResponse:
    body = (
        "# monitoraeo\n"
        "# We welcome AI training crawlers — being indexed is the point.\n"
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /report/\n"
        "Disallow: /preview/\n"
        "Disallow: /checkout/\n"
        "Disallow: /webhooks/\n"
        "\n"
        "User-agent: GPTBot\nAllow: /\n\n"
        "User-agent: ClaudeBot\nAllow: /\n\n"
        "User-agent: PerplexityBot\nAllow: /\n\n"
        "User-agent: Google-Extended\nAllow: /\n\n"
        "User-agent: anthropic-ai\nAllow: /\n\n"
        "User-agent: cohere-ai\nAllow: /\n\n"
        f"Sitemap: {SITE_BASE_URL}/sitemap.xml\n"
        f"# AI summary: {SITE_BASE_URL}/llms.txt\n"
    )
    return PlainTextResponse(body, media_type="text/plain")


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt() -> PlainTextResponse:
    """The emerging /llms.txt standard (https://llmstxt.org) — a markdown
    summary that tells LLM crawlers what monitoraeo is and which pages
    are authoritative content. Returns text/markdown so AI agents that
    sniff the content type recognise it."""
    body = f"""# monitoraeo

> AI Answer Engine Optimisation (AEO) and Generative Engine Optimisation (GEO) audits. \
We measure how often Claude, ChatGPT, Perplexity, Gemini and Google AI Overviews \
name a brand, cite its domain, and recommend it in buyer-facing answers — then \
turn the gaps into a fix list.

monitoraeo is the AEO/GEO audit platform for brands that want to be the answer when \
buyers ask AI a question. The product runs real-time queries across the five major \
AI answer engines, scores visibility, citation rate, sentiment, accuracy and \
hallucination risk per engine, and produces a prioritised action plan.

## Key concepts
- [What is AEO?]({SITE_BASE_URL}/what-is-aeo): Answer Engine Optimisation — the practice of getting your brand named, cited and recommended in AI answers from ChatGPT, Claude, Perplexity, Gemini and Google AI Overviews.
- [What is GEO?]({SITE_BASE_URL}/what-is-geo): Generative Engine Optimisation — the technical and content layer that makes a site retrievable, parseable and quotable by generative AI systems.
- [AEO vs SEO]({SITE_BASE_URL}/aeo-vs-seo): Where traditional SEO ends (ranking blue links) and AEO begins (winning the synthesised answer).

## Product
- [How it works]({SITE_BASE_URL}/how-it-works): A monitoraeo audit takes a domain, runs 40 buyer-facing queries across 5 engines (200 AI answers), and scores how each engine describes the brand.
- [Audit product]({SITE_BASE_URL}/product/audit): One-off diagnostic across all 5 AI engines.
- [Monitoring product]({SITE_BASE_URL}/product/monitoring): Monthly cron-scheduled audits with trend charts so you see drift over time.

## Pricing
- [Free preview]({SITE_BASE_URL}/#preview): 8 buyer-facing questions on Google AI Overviews. No account, ~30 seconds.
- [Two Engine Audit]({SITE_BASE_URL}/pricing): $29 one-off — Google AI + ChatGPT, 40 queries, competitor share-of-voice, hallucination flags, prioritised action plan.
- [Full Audit]({SITE_BASE_URL}/pricing): $79 one-off — all 5 engines (ChatGPT, Claude, Perplexity, Gemini, Google AI), 40 queries × 5 engines = 200 answers, prioritised action plan.
- [Two Engine Monitoring]({SITE_BASE_URL}/pricing): $35/mo — includes the Two Engine Audit, monthly re-runs, trend chart, prioritised action plan, 20 monitored buyer questions.
- [Full Monitoring]({SITE_BASE_URL}/pricing): $95/mo — includes the Full Audit, all 5 engines monitored monthly, per-engine trends, competitor + source change tracking, prioritised action plan, 40 monitored buyer questions.

Every paid tier includes the prioritised action plan (content, schema and entity fixes generated by Claude Sonnet 4.6 from the full audit result set). The free preview does not include the action plan.

## Engines covered
- Google AI Overviews (free preview tier and up)
- ChatGPT (Two Engine and up)
- Claude (Full and up)
- Perplexity (Full and up)
- Gemini (Full and up)

## What every audit measures
- Visibility — % of AI answers that name the brand
- Citation rate — % that link to the brand's domain as a source
- Competitor share-of-voice — who gets named or cited instead
- Hallucination flags — false claims AI engines make about the brand
- Sentiment + accuracy (paid tiers) — second-pass LLM scoring of every answer
- Technical foundations — 15 GEO checks across crawlability, structured data, metadata, content, performance and entity signals

## Helpful links
- [Pricing]({SITE_BASE_URL}/pricing)
- [Run a free preview]({SITE_BASE_URL}/#preview)
- [Support]({SITE_BASE_URL}/support)
- [Sitemap]({SITE_BASE_URL}/sitemap.xml)
"""
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8")


@app.get("/sitemap.xml")
def sitemap_xml() -> Response:
    today = datetime.now().strftime("%Y-%m-%d")
    urls = "\n".join(
        f"  <url>\n"
        f"    <loc>{SITE_BASE_URL}{path}</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{prio}</priority>\n"
        f"  </url>"
        for path, freq, prio in SITEMAP_PAGES
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


@app.get("/pricing", response_class=HTMLResponse)
def page_pricing(request: Request) -> HTMLResponse:
    return _render("pricing.html.j2", request=request,
                   breadcrumbs=[{"name": "Pricing", "path": "/pricing"}])


@app.get("/what-is-aeo", response_class=HTMLResponse)
def page_what_is_aeo(request: Request) -> HTMLResponse:
    return _render("what_is_aeo.html.j2", request=request,
                   breadcrumbs=[{"name": "What is AEO?", "path": "/what-is-aeo"}])


@app.get("/aeo-vs-seo", response_class=HTMLResponse)
def page_aeo_vs_seo(request: Request) -> HTMLResponse:
    return _render("aeo_vs_seo.html.j2", request=request,
                   breadcrumbs=[{"name": "AEO vs SEO", "path": "/aeo-vs-seo"}])


@app.get("/what-is-geo", response_class=HTMLResponse)
def page_what_is_geo(request: Request) -> HTMLResponse:
    return _render("what_is_geo.html.j2", request=request,
                   breadcrumbs=[{"name": "What is GEO?", "path": "/what-is-geo"}])


@app.get("/product/audit", response_class=HTMLResponse)
def page_product_audit(request: Request) -> HTMLResponse:
    return _render("product_audit.html.j2", request=request,
                   breadcrumbs=[
                       {"name": "Product", "path": "/product/audit"},
                       {"name": "Audit", "path": "/product/audit"},
                   ])


@app.get("/product/monitoring", response_class=HTMLResponse)
def page_product_monitoring(request: Request) -> HTMLResponse:
    return _render("product_monitoring.html.j2", request=request,
                   breadcrumbs=[
                       {"name": "Product", "path": "/product/audit"},
                       {"name": "Monitoring", "path": "/product/monitoring"},
                   ])


@app.get("/how-it-works", response_class=HTMLResponse)
def page_how_it_works(request: Request) -> HTMLResponse:
    return _render("how_it_works.html.j2", request=request,
                   breadcrumbs=[{"name": "How it works", "path": "/how-it-works"}])


@app.get("/privacy", response_class=HTMLResponse)
def page_privacy(request: Request) -> HTMLResponse:
    return _render("privacy.html.j2", request=request,
                   breadcrumbs=[{"name": "Privacy", "path": "/privacy"}])


@app.get("/terms", response_class=HTMLResponse)
def page_terms(request: Request) -> HTMLResponse:
    return _render("terms.html.j2", request=request,
                   breadcrumbs=[{"name": "Terms", "path": "/terms"}])


@app.get("/support", response_class=HTMLResponse)
def page_support(request: Request, status: str = "") -> HTMLResponse:
    return _render("support.html.j2", request=request, status=status or None,
                   breadcrumbs=[{"name": "Support", "path": "/support"}])


# Custom 404 for marketing/site routes. JSON API consumers (dashboard polling,
# webhooks) still get FastAPI's default {"detail": "..."} payload — we only
# swap in the styled HTML page when the request looks like it came from a
# browser. Returning the proper 404 status (not 200) is critical for SEO so
# Google deindexes dead URLs instead of treating them as soft-404s.
@app.exception_handler(404)
def _not_found_handler(request: Request, exc: HTTPException) -> Response:
    accept = request.headers.get("accept", "")
    path = request.url.path or ""
    wants_html = "text/html" in accept
    is_api = path.startswith(("/api/", "/dashboard/api/", "/webhooks/", "/preview/", "/report/"))
    if not wants_html or is_api:
        return JSONResponse({"detail": getattr(exc, "detail", "Not Found")}, status_code=404)
    html = _jinja.get_template("pages/not_found.html.j2").render(
        base_url=SITE_BASE_URL,
        user=None,
    )
    return HTMLResponse(html, status_code=404)


SUPPORT_TO_EMAIL = os.environ.get("SUPPORT_TO_EMAIL", "hello@example.com")


@app.post("/support", response_class=HTMLResponse)
def submit_support(
    email: str = Form(...),
    subject: str = Form(...),
    topic: str = Form("general"),
    message: str = Form(...),
) -> HTMLResponse:
    """Receive a support ticket and email it to SUPPORT_TO_EMAIL via Resend.
    Falls back to a graceful failure if Resend isn't configured yet."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sent = False
    if api_key:
        try:
            import resend
            resend.api_key = api_key
            from_addr = os.environ.get(
                "REPORT_FROM_EMAIL", "monitoraeo <reports@monitoraeo.com>"
            )
            body_html = (
                f"<p><strong>From:</strong> {email}</p>"
                f"<p><strong>Topic:</strong> {topic}</p>"
                f"<hr>"
                f"<p>{message.replace(chr(10), '<br>')}</p>"
            )
            resend.Emails.send({
                "from": from_addr,
                "to": [SUPPORT_TO_EMAIL],
                "reply_to": email,
                "subject": f"[Support · {topic}] {subject}",
                "html": body_html,
            })
            sent = True
        except Exception:  # noqa: BLE001
            sent = False
    return _render("support.html.j2", status="sent" if sent else "error")


@app.post("/monitoring/waitlist", response_class=HTMLResponse)
def monitoring_waitlist(
    email: str = Form(...),
    domain: str = Form(...),
) -> HTMLResponse:
    """Stub: store waitlist signups. For now, email them to support inbox."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if api_key:
        try:
            import resend
            resend.api_key = api_key
            from_addr = os.environ.get(
                "REPORT_FROM_EMAIL", "monitoraeo <reports@monitoraeo.com>"
            )
            resend.Emails.send({
                "from": from_addr,
                "to": [SUPPORT_TO_EMAIL],
                "subject": "[Waitlist] New Monitoring signup",
                "html": f"<p><strong>{email}</strong> · domain: {domain}</p>",
            })
        except Exception:  # noqa: BLE001
            pass
    return HTMLResponse(
        '<div style="font-family:Inter,system-ui;padding:60px;text-align:center;">'
        '<h1>You\'re on the list ✓</h1>'
        '<p>We\'ll email <strong>' + email + '</strong> when Monitoring opens up.</p>'
        '<p style="margin-top:20px;"><a href="/">← Back to home</a></p>'
        '</div>'
    )


# -----------------------------------------------------------------------------
# Free preview flow
# -----------------------------------------------------------------------------

def _normalise_domain(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    netloc = urlparse(raw).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split("/")[0]


def _brand_from_domain(domain: str) -> str:
    """Cheap brand-name guess from the domain — capitalise the first label."""
    label = domain.split(".")[0]
    label = re.sub(r"[-_]+", " ", label)
    return label.title()


def _generic_free_queries(brand: str, category: str | None) -> list[Query]:
    """Generates 8 buyer-facing questions that work for any brand. If a category
    is provided we use it to sharpen category/problem queries."""
    cat = (category or "").strip() or "this category"
    return [
        Query(query=f"What is {brand}?", type="brand", free=True),
        Query(query=f"Is {brand} legitimate?", type="brand", free=True),
        Query(query=f"{brand} reviews", type="brand", free=True),
        Query(query=f"Best {cat}", type="category", free=True),
        Query(query=f"Top {cat} 2026", type="category", free=True),
        Query(query=f"How do I choose a provider for {cat}?", type="problem", free=True),
        Query(query=f"Best alternatives to {brand}", type="comparison", free=True),
        Query(query=f"{brand} vs competitors", type="comparison", free=True),
    ]


def _generate_paid_queries(brand: str, competitors: list[str] | None = None) -> list[Query]:
    """Generate 40 brand-aware buyer-facing queries for a paid audit.

    Replaces the old config/queries.csv approach, which was hardcoded to a
    single seed brand ('Capify') — meaning every paid customer's report
    used to ask the AI engines about that brand instead of their own, and
    the hallucination scorer would flag everything because the answers
    didn't match the buyer's actual ground truth.

    No category required — every query references the brand directly OR a
    'similar to {brand}' phrasing so results stay on-topic without needing
    extra user input. Comparison queries lean on the customer-supplied
    competitor list when available; otherwise they fall back to generic
    'alternatives to' phrasings."""
    import sys as _sys
    print(f"[gen_queries] entered brand={brand!r} comps={competitors!r}", flush=True)
    _sys.stdout.flush()
    competitors = [c for c in (competitors or []) if c and c.strip()]
    queries: list[Query] = []

    # Brand (12) — direct questions about this brand.
    brand_qs = [
        f"What is {brand}?",
        f"Is {brand} legitimate?",
        f"{brand} reviews",
        f"{brand} pricing",
        f"How does {brand} work?",
        f"{brand} customer service contact",
        f"Pros and cons of {brand}",
        f"Is {brand} safe to use?",
        f"{brand} customer testimonials",
        f"{brand} support quality",
        f"{brand} features",
        f"{brand} setup process",
    ]
    queries += [Query(query=q, type="brand") for q in brand_qs]

    # Comparison (10) — head-to-head against named competitors; padded
    # with generic 'alternatives to {brand}' phrasings when fewer than 5
    # competitors were supplied. The pad list must be at least 10 items
    # so we can always reach 10 without duplicates — otherwise the
    # 'if fallback not in comp_qs' guard spins forever when the customer
    # supplies 0–2 competitors (2 comps × 2 = 4 real, + 4 unique pads = 8,
    # never reaches 10). That was an infinite loop in production.
    comp_qs: list[str] = []
    for c in competitors[:5]:
        comp_qs.append(f"{brand} vs {c}")
        comp_qs.append(f"{c} or {brand}: which is better?")
    fallback_pad = [
        f"Best alternatives to {brand}",
        f"Top {brand} alternatives 2026",
        f"Companies similar to {brand}",
        f"{brand} competitors compared",
        f"Sites like {brand}",
        f"Cheaper alternatives to {brand}",
        f"Better alternatives to {brand}",
        f"Free alternatives to {brand}",
        f"{brand} vs competitors review",
        f"What companies compete with {brand}",
        f"Who are {brand}'s biggest competitors",
        f"Best {brand}-style platforms",
    ]
    for fallback in fallback_pad:
        if len(comp_qs) >= 10:
            break
        if fallback not in comp_qs:
            comp_qs.append(fallback)
    queries += [Query(query=q, type="comparison") for q in comp_qs[:10]]

    # Category (10) — 'who else does what this brand does' style.
    cat_qs = [
        f"Best companies like {brand}",
        f"Top providers similar to {brand}",
        f"Who are the leaders in {brand}'s industry?",
        f"Best in class for what {brand} does",
        f"Companies that compete with {brand}",
        f"Industry leaders for {brand}-style services",
        f"Top-rated options similar to {brand}",
        f"Who else does what {brand} does?",
        f"Most recommended companies like {brand}",
        f"Trusted alternatives to {brand}",
    ]
    queries += [Query(query=q, type="category") for q in cat_qs]

    # Problem (8) — buyer-intent phrased.
    prob_qs = [
        f"How do I choose between {brand} and its competitors?",
        f"What should I look for in a {brand}-style service?",
        f"Is {brand} right for my business?",
        f"When should I use {brand} vs alternatives?",
        f"What are the risks of using {brand}?",
        f"How to evaluate {brand} for my needs",
        f"Should I trust {brand} reviews?",
        f"What questions to ask before using {brand}",
    ]
    queries += [Query(query=q, type="problem") for q in prob_qs]

    return queries[:40]


# Country code → display name for the loading-page + report header. The codes
# match Apify's `countryCode` param (ISO 3166-1 alpha-2, uppercase).
SUPPORTED_COUNTRIES: dict[str, str] = {
    "US": "United States",
    "AU": "Australia",
    "GB": "United Kingdom",
    "CA": "Canada",
    "NZ": "New Zealand",
    "IE": "Ireland",
    "SG": "Singapore",
    "IN": "India",
    "DE": "Germany",
    "FR": "France",
    "ES": "Spain",
    "IT": "Italy",
    "NL": "Netherlands",
    "BR": "Brazil",
    "JP": "Japan",
}

# TLD → country guess for when the user doesn't pick one. Generic TLDs
# (.com / .net / .org / .io / .ai) all fall through to US, which matches
# Google's behaviour when the user isn't geolocated.
_TLD_COUNTRY: dict[str, str] = {
    "com.au": "AU", "net.au": "AU", "org.au": "AU", "au": "AU",
    "co.uk": "GB", "org.uk": "GB", "uk": "GB",
    "ca": "CA",
    "co.nz": "NZ", "nz": "NZ",
    "ie": "IE",
    "com.sg": "SG", "sg": "SG",
    "co.in": "IN", "in": "IN",
    "de": "DE",
    "fr": "FR",
    "es": "ES",
    "it": "IT",
    "nl": "NL",
    "com.br": "BR", "br": "BR",
    "co.jp": "JP", "jp": "JP",
}


def _country_from_tld(domain: str) -> str:
    """Best-effort country guess from a domain's TLD. Returns an ISO 3166-1
    alpha-2 code (uppercase). Falls back to 'US' for generic TLDs."""
    d = (domain or "").lower().strip().rstrip("/")
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0]
    parts = d.split(".")
    # Try 2-segment TLDs first (e.g., com.au, co.uk) then 1-segment.
    if len(parts) >= 2:
        two = ".".join(parts[-2:])
        if two in _TLD_COUNTRY:
            return _TLD_COUNTRY[two]
    if parts:
        one = parts[-1]
        if one in _TLD_COUNTRY:
            return _TLD_COUNTRY[one]
    return "US"


def _resolve_country(domain: str, user_country: str | None) -> str:
    """Honour an explicit pick when it's a supported code; otherwise guess
    from the TLD."""
    code = (user_country or "").strip().upper()
    if code and code in SUPPORTED_COUNTRIES:
        return code
    return _country_from_tld(domain)


def _build_preview_site(domain: str, brand_name: str, country: str = "US") -> SiteConfig:
    return SiteConfig(
        brand=BrandConfig(name=brand_name, domain=domain, aliases=[domain]),
        competitors=[],
        ground_truth=[],
        locale=LocaleConfig(country=country, language="en"),
        engines=EnginesConfig(
            openrouter=[],
            apify=[ApifyEngineConfig(label=FREE_TIER_ENGINE)],
        ),
    )


def _set_step(run_id: str, step: str, pct: int | None = None) -> None:
    """Publish a human-readable progress step + optional pct to the loading page."""
    job = PREVIEW_JOBS.get(run_id, {})
    job["status"] = "running"
    job["step"] = step
    if pct is not None:
        job["pct"] = pct
    PREVIEW_JOBS[run_id] = job


def _add_finding(run_id: str, finding: str) -> None:
    """Append a live finding (e.g. 'Found 3 competitors in AI answers') to the
    job blob. The loading page polls /preview/{id}/status and splices new
    findings into its rotating info card so users see real audit signal as it
    emerges, not just generic stats."""
    job = PREVIEW_JOBS.get(run_id, {})
    findings = list(job.get("findings") or [])
    finding = finding.strip()
    if finding and finding not in findings:
        findings.append(finding)
        job["findings"] = findings
        PREVIEW_JOBS[run_id] = job


def _run_preview_job(
    run_id: str, domain: str, brand_name: str, category: str | None, country: str = "US"
) -> None:
    """Background worker for a free preview. Updates PREVIEW_JOBS as it progresses."""
    _set_step(run_id, "Pulling your site snapshot…", 8)
    try:
        site = _build_preview_site(domain, brand_name, country=country)
        queries = _generic_free_queries(brand_name, category)
        # Persist preview metadata so the dashboard "claim" flow can hydrate
        # a TrackedBrand from a logged-out user's preview after they sign up.
        try:
            run_dir_meta = OUTPUT_ROOT / run_id
            run_dir_meta.mkdir(parents=True, exist_ok=True)
            (run_dir_meta / "preview_meta.json").write_text(
                json.dumps({
                    "run_id": run_id,
                    "domain": domain,
                    "brand_name": brand_name,
                    "category": category,
                    "country": country,
                    "created_at": datetime.utcnow().isoformat(),
                })
            )
        except OSError:
            pass
        engine_objs = [
            ApifyEngine(
                label=FREE_TIER_ENGINE,
                country_code=site.locale.country,
                language_code=site.locale.language,
            )
        ]
        run_dir = OUTPUT_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = capture_screenshot(domain, run_dir)

        _set_step(run_id, f"Gathering AI answers across {len(queries)} buyer questions…", 25)

        async def _gather():
            return await asyncio.gather(
                run_audit(engine_objs, queries, run_dir),
                run_tech_audit_async(domain),
            )

        responses, tech = asyncio.run(_gather())

        # Surface a couple of tech-audit findings to the loading page so the
        # user sees real signal while the rest of the audit finishes. Keyed by
        # the stable .id field — title strings are user-facing and may move.
        try:
            checks = {c.id: c for c in (tech.checks if tech else [])}
            llms = checks.get("t1_llms_txt")
            if llms and llms.status == "fail":
                _add_finding(run_id, "Your site doesn't have an llms.txt file — fewer than 2% of websites do.")
            org = checks.get("t2_org_schema")
            if org and org.status == "fail":
                _add_finding(run_id, "We didn't find Organization JSON-LD on your homepage — AI engines lean on it to identify who you are.")
        except Exception:  # noqa: BLE001
            pass

        _set_step(run_id, "Identifying competitors named in the answers…", 70)

        # Auto-extract competitors from the responses via Haiku, then inject
        # into the SiteConfig so the deterministic scorer can flag them in the
        # text + citations. ~$0.001 per preview, non-fatal on failure.
        try:
            site.competitors = asyncio.run(
                extract_competitors(responses, brand_name)
            )
        except Exception:  # noqa: BLE001
            site.competitors = []

        if site.competitors:
            top = [c for c in site.competitors[:3] if c]
            if len(top) >= 2:
                _add_finding(
                    run_id,
                    f"Google AI is naming {', '.join(top[:-1])} and {top[-1]} when answering buyer questions in your category.",
                )
            elif top:
                _add_finding(
                    run_id,
                    f"Google AI is naming {top[0]} as a competitor in your category.",
                )

        _set_step(run_id, "Compiling visibility and citation scores…", 85)
        rows = [
            ScoredRow(
                response=r,
                deterministic=score_response(r, site),
                llm=LLMScore(),
            )
            for r in responses
        ]
        write_csv(rows, run_dir)

        # Final visibility finding — surfaces the "headline" number before the
        # report opens, so the user already has a hook in their head.
        try:
            total = len(rows)
            mentioned = sum(1 for r in rows if r.deterministic.mentioned)
            if total:
                if mentioned == 0:
                    _add_finding(run_id, f"Google AI didn't mention {brand_name} once across {total} buyer questions.")
                else:
                    _add_finding(run_id, f"Google AI mentioned {brand_name} in {mentioned} of {total} buyer questions.")
        except Exception:  # noqa: BLE001
            pass

        _set_step(run_id, "Assembling your report…", 95)
        write_html(
            rows,
            site,
            run_dir,
            tier="free",
            screenshot=screenshot_path.name if screenshot_path else None,
            tech=tech,
        )
        PREVIEW_JOBS[run_id] = {"status": "ready", "run_dir": str(run_dir)}
    except Exception as exc:  # noqa: BLE001
        PREVIEW_JOBS[run_id] = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _smart_titlecase(text: str) -> str:
    """Title-case a brand name ONLY when the user gave it to us all
    lowercase. Preserves intentionally-styled brands ('JB Hi-Fi', 'iPhone',
    'L'Oréal', 'monitoraeo') which carry at least one uppercase letter as a
    signal that the casing was deliberate.

    Handles apostrophes correctly (re.sub on word-boundary letters), unlike
    str.title() which would turn 'john's dental' into 'John'S Dental'."""
    text = (text or "").strip()
    if not text or any(c.isupper() for c in text):
        return text
    return re.sub(r"\b\w", lambda m: m.group().upper(), text)


def _start_preview(
    domain: str, brand: str, category: str | None, country: str | None = None
) -> tuple[str, str, str, str]:
    """Validate inputs, kick off a background preview run.
    Returns (run_id, normalised_domain, brand, resolved_country).
    Raises HTTPException(400) on bad input."""
    norm = _normalise_domain(domain)
    if not norm or "." not in norm:
        raise HTTPException(400, "Please enter a valid domain (e.g. capify.com.au)")
    brand = _smart_titlecase(brand)
    if not brand:
        raise HTTPException(
            400,
            "Brand name is required — type it exactly as you brand it "
            "(e.g. 'JB Hi-Fi', not 'jbhifi'). We use this to match your name "
            "in AI answers.",
        )
    resolved_country = _resolve_country(norm, country)
    run_id = _make_run_id(brand)
    PREVIEW_JOBS[run_id] = {"status": "queued", "step": "Starting up…"}
    threading.Thread(
        target=_run_preview_job,
        args=(run_id, norm, brand, (category or "").strip() or None, resolved_country),
        daemon=True,
    ).start()
    return run_id, norm, brand, resolved_country


@app.post("/preview", response_class=HTMLResponse)
def submit_preview(
    domain: str = Form(...),
    brand_name: str = Form(...),
    category: str = Form(""),
    country: str = Form(""),
) -> HTMLResponse:
    run_id, norm, brand, resolved_country = _start_preview(
        domain, brand_name, category, country
    )
    html = _jinja.get_template("loading.html.j2").render(
        run_id=run_id,
        brand_name=brand,
        domain=norm,
        country_code=resolved_country,
        country_name=SUPPORTED_COUNTRIES.get(resolved_country, resolved_country),
    )
    return HTMLResponse(html)


def _resolve_teaser_shortlink(d_param: str) -> tuple[str, str, str] | None:
    """If `d` looks like a TeaserShortlink id (pure digits), look it up and
    return (domain, brand, category). Returns None when it's not numeric
    or the row is missing — caller falls back to treating `d` as a literal
    domain (the old long-URL behaviour)."""
    if not d_param or not d_param.isdigit():
        return None
    try:
        from sqlmodel import select as _select
        from src.db import TeaserShortlink, get_session
        with get_session() as s:
            row = s.exec(
                _select(TeaserShortlink).where(TeaserShortlink.id == int(d_param))
            ).first()
            if row:
                return row.domain, row.brand, (row.category or "")
    except Exception as exc:  # noqa: BLE001
        print(f"[teaser-shortlink] lookup failed: {type(exc).__name__}: {exc}")
    return None


@app.get("/preview", response_class=HTMLResponse)
def submit_preview_get(
    d: str = "",
    b: str = "",
    c: str = "",
    co: str = "",
    domain: str = "",
    brand: str = "",
    category: str = "",
    country: str = "",
) -> HTMLResponse:
    """Cold-email entry point. Accepts:
      - `d=<short-id>` → resolves to the full domain via TeaserShortlink, and
        if the caller didn't pass `b`/`c` we hydrate brand + category from
        the same row (so a recipient can click a stripped-down `?d=42` link).
      - `d=<domain>` or `domain=…` → legacy long form, still supported.
      - `b`/`brand`, `c`/`category`, `co`/`country` → short or long aliases.

    Whenever the visitor came via a teaser shortlink (`d` was numeric and
    resolved) we tag the response with attribution context: a JS dataLayer
    push that GTM forwards to GA4 as a campaign-attributed event. The
    attribution ONLY fires on this initial /preview hit — normal landing
    visits, manual /preview submissions and direct navigation are never
    attributed to email/outreach."""
    resolved_domain = d
    resolved_brand = b
    resolved_category = c
    came_via_shortlink = False
    looked_up = _resolve_teaser_shortlink(d)
    if looked_up is not None:
        resolved_domain = looked_up[0]
        # Query-string values still win when supplied — lets the same shortlink
        # be reused with a different brand label if you ever need to.
        resolved_brand = b or looked_up[1]
        resolved_category = c or looked_up[2]
        came_via_shortlink = True
    run_id, norm, real_brand, resolved_country = _start_preview(
        resolved_domain or domain,
        resolved_brand or brand,
        resolved_category or category,
        co or country,
    )
    html = _jinja.get_template("loading.html.j2").render(
        run_id=run_id,
        brand_name=real_brand,
        domain=norm,
        country_code=resolved_country,
        country_name=SUPPORTED_COUNTRIES.get(resolved_country, resolved_country),
        # GA4 auto-buckets medium='email' into the default Email channel; if
        # we ever want to distinguish outreach from nurture/transactional we
        # can split via campaign_name without changing medium.
        traffic_source=("email" if came_via_shortlink else None),
        traffic_medium=("email" if came_via_shortlink else None),
        traffic_campaign=("teaser_outreach" if came_via_shortlink else None),
    )
    return HTMLResponse(html)


@app.get("/preview/{run_id}/status")
def preview_status(run_id: str) -> JSONResponse:
    job = PREVIEW_JOBS.get(run_id)
    if not job:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return JSONResponse(job)


# ---------------------------------------------------------------------------
# Cold-email teaser API
# ---------------------------------------------------------------------------

def _require_teaser_token(request: Request) -> None:
    """Bearer-token gate for the /api/teaser* endpoints. Reads the expected
    secret from TEASER_API_TOKEN. Compares with secrets.compare_digest so
    short-circuit timing doesn't leak the secret. Returns 503 when the env
    var isn't set on the server (so a forgotten config doesn't silently
    leave the endpoint open) and 401 when the caller sends the wrong /
    missing token."""
    expected = os.environ.get("TEASER_API_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "TEASER_API_TOKEN is not configured on the server")
    header = request.headers.get("authorization", "").strip()
    presented = ""
    if header.lower().startswith("bearer "):
        presented = header[7:].strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(401, "Invalid or missing bearer token")


class TeaserRequest(BaseModel):
    domain: str
    brand: str
    category: str | None = None
    # When true, delete the cached site screenshot + teaser image before
    # regenerating. Use this after deploying a teaser-design change or
    # after rotating SCREENSHOTAPI_TOKEN. Idempotent + safe.
    force: bool = False
    # "static" (default): skip Apify + Haiku entirely, hardcode the image
    # labels to VISIBILITY=POOR / COMPETITORS=YES, use generic curiosity
    # copy. ~10x cheaper per call (~$0.001 vs ~$0.0085). Designed for
    # 100k+/month cold-outreach volume where we'd burn ~$850/mo on Apify
    # otherwise.
    # "live": run the real Apify + Haiku query, render per-prospect labels,
    # compose body with specific competitor claims. Use for low-volume
    # hand-curated sends where accuracy matters more than cost.
    mode: str = "static"


@app.get("/teasers/{filename}")
def serve_teaser(filename: str):
    """Serve a generated teaser image. Filename is a content hash so callers
    can cache aggressively in their own systems."""
    from fastapi.responses import FileResponse
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "", filename)
    if not safe.endswith(".png"):
        raise HTTPException(404, "Not found")
    path = OUTPUT_ROOT / "teasers" / safe
    if not path.exists():
        raise HTTPException(404, "Teaser not found")
    return FileResponse(path, media_type="image/png", headers={
        "Cache-Control": "public, max-age=2592000, immutable",  # 30 days
    })


def _build_teaser_payload(req: TeaserRequest) -> dict[str, Any]:
    """Shared implementation behind /api/teaser and /api/teaser/email.
    Runs the Apify query, extracts competitors, captures the screenshot,
    composes the hero image, and returns the JSON payload as a plain dict
    so the email-copy endpoint can decorate it with subject + body."""
    import hashlib
    from urllib.parse import urlencode

    norm = _normalise_domain(req.domain)
    if not norm or "." not in norm:
        raise HTTPException(400, "Invalid domain")
    brand = _smart_titlecase(req.brand)
    if not brand:
        raise HTTPException(400, "brand is required")
    category = (req.category or "").strip()
    is_static = (req.mode or "static").lower() != "live"

    teaser_query = (
        f"questions about {brand}" if is_static
        else (f"best {category}" if category else f"What is {brand}?")
    )

    if is_static:
        # Static mode: no Apify, no Haiku. Image gets hardcoded "POOR" / "YES"
        # labels regardless of actual visibility, body uses generic curiosity
        # copy (no false-claim risk because nothing specific is asserted).
        visibility_pct = 0.0
        competitors = []
        brand_in_text = False
        brand_in_citations = False
    else:
        apify = ApifyEngine(
            label=FREE_TIER_ENGINE,
            country_code="US",
            language_code="en",
        )
        response = asyncio.run(apify.query(teaser_query, "category" if category else "brand"))

        # Did the brand appear in the response?
        text_lower = (response.response_text or "").lower()
        brand_lower = brand.lower()
        brand_in_text = brand_lower in text_lower
        brand_in_citations = any(
            norm.lower() in (c.domain or "").lower() for c in response.citations
        )
        visibility_pct = 100.0 if (brand_in_text or brand_in_citations) else 0.0

        # Extract competitors from the single response
        try:
            competitors = asyncio.run(extract_competitors([response], brand))
        except Exception:  # noqa: BLE001
            competitors = []

    # Site screenshot — best effort, non-fatal
    teasers_dir = OUTPUT_ROOT / "teasers"
    teasers_dir.mkdir(parents=True, exist_ok=True)
    shot_dir = teasers_dir / "_screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot_filename = f"{hashlib.md5(norm.encode()).hexdigest()[:12]}.png"
    site_screenshot = shot_dir / shot_filename
    # When force=True, bust the screenshot cache too so we re-capture with
    # whatever credentials are currently configured.
    if req.force and site_screenshot.exists():
        try:
            site_screenshot.unlink()
        except OSError:
            pass
    if not site_screenshot.exists():
        try:
            captured = capture_screenshot(norm, shot_dir, filename=shot_filename)
            if not (captured and captured.exists()):
                site_screenshot = None
        except Exception:  # noqa: BLE001
            site_screenshot = None

    # Compose the teaser image. Hash includes mode so a domain teased in
    # both 'live' and 'static' caches to separate files. The leading 'v3'
    # is a cache-bust prefix — bump it whenever the rendered image format
    # changes (tile labels, layout, etc.) so any stale files on disk get
    # bypassed instead of served. v1 = original % tiles, v2 = YES/NO
    # binary, v3 = static POOR/YES tiles.
    from src.teaser_image import generate as generate_teaser
    brand_key = brand.strip().lower()
    img_hash = hashlib.md5(
        f"v3|{norm}|{brand_key}|{'static' if is_static else 'live'}|"
        f"{visibility_pct}|{','.join(competitors[:3])}".encode()
    ).hexdigest()[:16]
    img_filename = f"{img_hash}.png"
    img_path = teasers_dir / img_filename
    # Cache hit: identical (domain, brand, mode, data) → reuse existing file.
    # Important at 100k+/month volume — a re-send to the same prospect or
    # the same domain in static mode costs zero CPU + zero API.
    if img_path.exists() and not req.force:
        pass
    else:
        try:
            generate_teaser(
                brand_name=brand,
                domain=norm,
                visibility_pct=visibility_pct,
                competitors=competitors,
                site_screenshot=site_screenshot if (site_screenshot and site_screenshot.exists()) else None,
                output_path=img_path,
                category=category or None,
                static_mode=is_static,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Teaser image generation failed: {exc}")

    # mtime-based cache-buster so email clients + browsers fetch the new
    # image after each regen, even though the path is content-hashed.
    try:
        ver = int(img_path.stat().st_mtime)
    except OSError:
        ver = 0

    # Mint a short-link so the cold-email URL is /preview?d=42&b=… instead of
    # repeating the full domain in the query string. Reuses an existing row
    # for the same (domain, brand, category) so a re-send to the same
    # prospect doesn't bloat the table — at 100k+/month volume a unique row
    # per send would mean ~1.2M rows/year for the same handful of brands.
    # Fail-soft: if the DB isn't reachable we fall back to the legacy long
    # form so the endpoint still works in dev / when Postgres is down.
    short_id: int | None = None
    try:
        from sqlmodel import select as _select
        from src.db import TeaserShortlink, get_session
        with get_session() as s:
            existing = s.exec(
                _select(TeaserShortlink)
                .where(TeaserShortlink.domain == norm)
                .where(TeaserShortlink.brand == brand)
                .where(TeaserShortlink.category == (category or None))
            ).first()
            if existing:
                short_id = existing.id
            else:
                row = TeaserShortlink(
                    domain=norm, brand=brand, category=category or None,
                )
                s.add(row)
                s.commit()
                s.refresh(row)
                short_id = row.id
    except Exception as exc:  # noqa: BLE001
        print(f"[teaser-shortlink] mint failed, falling back to long URL: "
              f"{type(exc).__name__}: {exc}")

    if short_id is not None:
        # Keep the brand in the URL so the recipient hovering the link sees
        # something readable ("...?d=42&b=Famous+Hollywood+Dental+Care").
        click_qs = urlencode({"d": str(short_id), "b": brand})
    else:
        click_qs = urlencode(
            {"d": norm, "b": brand, "c": category} if category
            else {"d": norm, "b": brand}
        )
    click_url = f"{SITE_BASE_URL}/preview?{click_qs}"

    return {
        "mode": "static" if is_static else "live",
        "category": category,
        "brand_name": brand,
        "domain": norm,
        "visibility_pct": visibility_pct,
        "competitors": competitors,
        "teaser_image_url": f"{SITE_BASE_URL}/teasers/{img_filename}?v={ver}",
        "click_url": click_url,
        "teaser_query": teaser_query,
        "answered_in": ["text" if brand_in_text else None, "citations" if brand_in_citations else None],
    }


@app.post("/api/teaser")
def api_teaser(req: TeaserRequest, request: Request) -> JSONResponse:
    """Cold-email integration point. Runs ONE Apify query, extracts competitors
    via Haiku, generates a hero image, and returns the asset URLs your email
    sender can drop straight into the message body.

    Auth: requires `Authorization: Bearer <TEASER_API_TOKEN>` header.
    Cost: ~$0.0085 per call (1 Apify SERP + 1 Haiku extraction).
    Returns: {teaser_image_url, click_url, brand_name, domain, visibility_pct, competitors}
    """
    _require_teaser_token(request)
    return JSONResponse(_build_teaser_payload(req))


def _ensure_leading_capital(s: str) -> str:
    """Guarantee the first character is uppercase. Safer than str.capitalize()
    which would lowercase the rest (and mangle 'Google AI' → 'Google ai')."""
    return (s[0].upper() + s[1:]) if s else s


def _compose_outreach_subject(data: dict[str, Any]) -> str:
    """Pick a cold-email subject line tailored to the teaser result.
    Stays under 60 characters so the full line shows in most inbox previews.
    Always returns a string starting with a capital letter — defensive
    against brand names that slipped through without _smart_titlecase.

    Static mode: question form, no specific claims (we didn't actually run
    the query so we can't truthfully assert anything about findings).
    Live mode: specific competitor-naming or visibility hooks."""
    brand = data.get("brand_name") or "your brand"
    if data.get("mode") == "static":
        category = (data.get("category") or "").strip()
        if category:
            return _ensure_leading_capital(
                f"Is {brand} showing up when buyers ask about {category}?"
            )
        return _ensure_leading_capital(f"Is {brand} appearing in Google AI Overviews?")
    competitors = data.get("competitors") or []
    visible = bool(data.get("visibility_pct"))
    top = competitors[0] if competitors else ""
    second = competitors[1] if len(competitors) > 1 else ""
    if not visible and top and second:
        out = f"Google AI is naming {top} and {second}, not {brand}"
    elif not visible and top:
        out = f"Google AI is naming {top}, not {brand}"
    elif not visible:
        out = f"Google AI doesn't mention {brand} when buyers ask"
    elif visible and top:
        out = f"{brand} shows up in Google AI — but so does {top}"
    else:
        out = f"{brand} is in Google AI — what about ChatGPT and Claude?"
    return _ensure_leading_capital(out)


def _compose_outreach_blocks(data: dict[str, Any]) -> dict[str, str]:
    """Build the structured email blocks the outreach tool composes the
    message from. Each block is plain text — drop them into your template in
    this order:

        {{hook}}
        [image: {{image_url}}]
        {{proof}}
        [button: {{cta_text}} → {{cta_url}}]
        {{signature}}

    The legacy `body` field is also returned (assembled from these blocks)
    so existing callers keep working.

    Static mode emits generic curiosity copy with no specific findings
    claimed (because we never ran the query). Live mode references the
    actual visibility/competitor data."""
    brand = data.get("brand_name") or "your brand"
    click_url = data.get("click_url") or ""
    image_url = data.get("teaser_image_url") or ""
    signature = "Liam Carter\nAEO Specialist\nmonitoraeo.com"
    cta_text = "See the full snapshot"

    if data.get("mode") == "static":
        category = (data.get("category") or "").strip()
        about_cat = f" about {category}" if category else " your buyers are asking"
        hook = (
            f"Hi,\n\n"
            f"I ran a quick AI visibility check on {brand} to see how "
            f"Google's AI answers questions{about_cat}."
        )
        proof = (
            "The snapshot below shows what's happening — including which "
            "competitors are getting named alongside you."
        )
        body = (
            f"{hook}\n\n"
            f"{proof}\n\n"
            f"Full snapshot here:\n{click_url}\n\n"
            f"The monitoraeo audit (40 questions × 5 AI engines = 200 "
            f"answers) shows you exactly which gaps to fix.\n\n"
            f"{signature}"
        )
        return {
            "hook": hook,
            "image_url": image_url,
            "proof": proof,
            "cta_url": click_url,
            "cta_text": cta_text,
            "signature": signature,
            "body": body,
        }

    # Live mode — reference real findings.
    query = data.get("teaser_query") or "questions in your category"
    competitors = data.get("competitors") or []
    visible = bool(data.get("visibility_pct"))
    top3 = competitors[:3]
    visibility_line = (
        f"It does mention {brand}." if visible
        else f"It didn't mention {brand} once."
    )
    if top3 and not visible:
        comp_line = f"It pointed buyers at {', '.join(top3)} instead."
    elif top3 and visible:
        comp_line = f"But it also named {', '.join(top3)} in the same answer."
    else:
        comp_line = "The category looks wide open — no clear competitors showed up."

    hook = (
        f"Hi,\n\n"
        f"I asked Google AI Overviews \"{query}\" — the kind of question "
        f"your buyers are typing — to see how it answers."
    )
    proof = f"{visibility_line} {comp_line}"
    body = (
        f"{hook}\n\n"
        f"{proof}\n\n"
        f"Full snapshot here:\n{click_url}\n\n"
        f"If the AI isn't pointing buyers at {brand}, the monitoraeo audit "
        f"(40 questions × 5 AI engines = 200 answers) shows you exactly "
        f"which gaps to fix.\n\n"
        f"{signature}"
    )

    return {
        "hook": hook,
        "image_url": image_url,
        "proof": proof,
        "cta_url": click_url,
        "cta_text": cta_text,
        "signature": signature,
        "body": body,
    }


@app.post("/api/teaser/email")
def api_teaser_email(req: TeaserRequest, request: Request) -> JSONResponse:
    """Same as /api/teaser plus a tailored cold-email subject and structured
    body blocks (hook / image_url / proof / cta_url / cta_text / signature).
    Compose the message in your outreach tool like:

        Subject: {{subject}}

        {{hook}}
        <img src="{{image_url}}">
        {{proof}}
        <a href="{{cta_url}}">{{cta_text}}</a>
        {{signature}}

    The legacy `body` string is still returned so existing integrations
    keep working.

    Auth: requires `Authorization: Bearer <TEASER_API_TOKEN>` header."""
    _require_teaser_token(request)
    data = _build_teaser_payload(req)
    data["subject"] = _compose_outreach_subject(data)
    data.update(_compose_outreach_blocks(data))
    return JSONResponse(data)


def _safe_return_to(url: str) -> str:
    """Only honour return_to URLs on the same site, to avoid /checkout/cancel
    being abused as an open redirect. Falls back to '' (→ home) otherwise."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if not parsed.netloc:
        # Path-only — safe to honour.
        return url if url.startswith("/") else ""
    # Allowed hosts: the configured public base plus its bare apex/www variants.
    allowed = set()
    base = urlparse(PUBLIC_BASE_URL)
    if base.netloc:
        allowed.add(base.netloc.lower())
        bare = base.netloc.lower().removeprefix("www.")
        allowed.add(bare)
        allowed.add("www." + bare)
    return url if parsed.netloc.lower() in allowed else ""


@app.post("/checkout")
def create_checkout(req: CheckoutRequest) -> JSONResponse:
    """Creates a Stripe Checkout Session for the chosen tier. Picks payment
    vs subscription mode based on the tier plan."""
    if req.tier not in TIER_PLANS:
        raise HTTPException(
            400, f"Unknown tier {req.tier!r}. Valid: {sorted(TIER_PLANS)}"
        )
    plan = TIER_PLANS[req.tier]
    price_id = os.environ.get(plan["stripe_env"], "").strip()
    if not price_id:
        raise HTTPException(
            500, f"No {plan['stripe_env']} env var configured"
        )
    if not stripe.api_key:
        raise HTTPException(500, "STRIPE_SECRET_KEY is not set")

    session = stripe.checkout.Session.create(
        mode=plan["stripe_mode"],  # "payment" (one-off) or "subscription" (monthly)
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=req.email,
        allow_promotion_codes=True,
        success_url=f"{PUBLIC_BASE_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_BASE_URL}/checkout/cancel?session_id={{CHECKOUT_SESSION_ID}}",
        metadata={
            "tier": req.tier,
            "brand_name": req.brand_name,
            "domain": req.domain,
            "email": req.email,
        },
    )
    return JSONResponse({"id": session.id, "url": session.url})


@app.post("/buy")
def buy_redirect(
    request: Request,
    tier: str = Form(...),
    brand_name: str = Form(""),
    domain: str = Form(""),
    return_to: str = Form(""),
):
    """Form-POST entry point used by the in-report tier cards. Creates a
    Stripe Checkout Session for the chosen tier and 303-redirects the
    browser to the Stripe-hosted page. Email is collected by Stripe
    (we don't have it yet for cold-email visitors)."""
    if tier not in TIER_PLANS:
        raise HTTPException(400, f"Unknown tier {tier!r}")
    plan = TIER_PLANS[tier]
    price_id = os.environ.get(plan["stripe_env"], "").strip()
    if not price_id:
        raise HTTPException(500, f"No {plan['stripe_env']} env var configured")
    if not stripe.api_key:
        raise HTTPException(500, "STRIPE_SECRET_KEY is not set")
    # Track where the buyer came from so /checkout/cancel can bounce them
    # back. Explicit hidden field wins; Referer is the fallback.
    origin = (return_to or request.headers.get("referer") or "").strip()
    session = stripe.checkout.Session.create(
        mode=plan["stripe_mode"],
        line_items=[{"price": price_id, "quantity": 1}],
        allow_promotion_codes=True,
        success_url=f"{PUBLIC_BASE_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_BASE_URL}/checkout/cancel?session_id={{CHECKOUT_SESSION_ID}}",
        metadata={
            "tier": tier,
            "brand_name": brand_name.strip(),
            "domain": domain.strip(),
            "return_to": _safe_return_to(origin),
        },
    )
    return RedirectResponse(session.url, status_code=303)


def _to_plain_dict(obj: Any) -> dict[str, Any]:
    """Convert a stripe StripeObject (or anything mapping-shaped) to a plain
    nested dict. stripe-python 8.0+ stopped inheriting StripeObject from
    dict, so calling .get() on a Session/Invoice/Event raises
    AttributeError: 'get'. Doing this conversion once at the boundary lets
    the rest of the code use ordinary dict.get(...) safely."""
    if obj is None:
        return {}
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive() or {}
    if hasattr(obj, "to_dict"):
        return obj.to_dict() or {}
    try:
        return dict(obj)
    except (TypeError, ValueError):
        return {}


def _meta_with_email(meta: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """Stripe-collected emails live on session.customer_email or
    session.customer_details.email — not in the metadata we set. Fold them
    into the meta dict so /checkout/success and _fulfil_order both work
    when we don't pre-supply email at session-create time. Expects a plain
    dict — call _to_plain_dict(stripe_session) first."""
    out = dict(meta or {})
    if not out.get("email"):
        ce = (session.get("customer_email") or "")
        if not ce:
            ce = ((session.get("customer_details") or {}).get("email") or "")
        out["email"] = (ce or "").strip()
    return out


@app.get("/checkout/success", response_class=HTMLResponse)
def checkout_success(session_id: str | None = None) -> HTMLResponse:
    """After Stripe redirects back, ask the customer for their competitor list
    before kicking off the audit. The webhook only registers the order; this
    page (or its form submit) is what actually starts the run."""
    # If the webhook hasn't landed yet, fall back to fetching from Stripe directly.
    meta: dict[str, Any] = {}
    if session_id and session_id in PENDING_ORDERS:
        meta = PENDING_ORDERS[session_id]
    elif session_id and stripe.api_key:
        try:
            sess_dict = _to_plain_dict(stripe.checkout.Session.retrieve(session_id))
            meta = _meta_with_email(sess_dict.get("metadata") or {}, sess_dict)
            PENDING_ORDERS[session_id] = meta
        except Exception:  # noqa: BLE001
            meta = {}

    brand = meta.get("brand_name") or "your brand"
    domain = meta.get("domain") or ""
    tier = meta.get("tier") or ""
    sid = session_id or ""
    plan = TIER_PLANS.get(tier, {})
    # Strip the "(... $29)" suffix so GA reports a clean item name.
    raw_label = plan.get("label") or tier
    tier_label = raw_label.split(" (")[0] if " (" in raw_label else raw_label
    return HTMLResponse(_jinja.get_template("checkout_setup.html.j2").render(
        brand=brand, domain=domain, tier=tier, session_id=sid,
        tier_label=tier_label,
        tier_price_usd=plan.get("price_usd") or 0,
        tier_is_subscription=plan.get("stripe_mode") == "subscription",
    ))


@app.post("/orders/setup")
def orders_setup(
    session_id: str = Form(...),
    competitor_1: str = Form(""),
    competitor_2: str = Form(""),
    competitor_3: str = Form(""),
    competitor_4: str = Form(""),
    competitor_5: str = Form(""),
) -> RedirectResponse:
    """Receive the post-payment setup form, attach competitors to the order
    metadata, fire the audit in the background, then bounce the user to a
    dedicated 'order received — check your email' page (NOT the generic
    sign-in page — that felt like a mistake for new paying customers).
    The audit completes in ~30s; by the time they click the magic link in
    their inbox, the report is ready."""
    meta = PENDING_ORDERS.get(session_id)
    if not meta:
        if not session_id or not stripe.api_key:
            raise HTTPException(404, "Unknown session")
        try:
            sess_dict = _to_plain_dict(stripe.checkout.Session.retrieve(session_id))
            meta = _meta_with_email(sess_dict.get("metadata") or {}, sess_dict)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, f"Could not load session: {type(exc).__name__}: {exc}")

    competitors = [
        c.strip() for c in (competitor_1, competitor_2, competitor_3, competitor_4, competitor_5)
        if c and c.strip()
    ]
    meta = dict(meta)  # don't mutate the registry entry directly
    meta["competitors"] = competitors
    buyer_email = (meta.get("email") or "").strip()
    brand_name = (meta.get("brand_name") or "").strip()
    domain = (meta.get("domain") or "").strip()
    tier = (meta.get("tier") or "").strip()

    # Defensive: persist the order meta to disk before kicking anything off.
    # If hydration silently fails downstream, we can still recover the order
    # later — by hand or via /dashboard/recover-paid-orders.
    try:
        pending_dir = OUTPUT_ROOT / "_paid_orders"
        pending_dir.mkdir(parents=True, exist_ok=True)
        (pending_dir / f"{session_id}.json").write_text(json.dumps({
            **meta,
            "received_at": datetime.utcnow().isoformat(),
        }))
    except Exception as exc:  # noqa: BLE001
        print(f"[orders/setup] failed to persist order meta for {session_id}: {exc}")

    # Send the magic-link email IMMEDIATELY — before the audit runs and
    # independent of hydration. Customers were waiting 30+ seconds for an
    # email that never arrived because magic-link send used to live at the
    # end of _ensure_dashboard_for_paid_order, so any upstream failure
    # killed it. Now it fires right after the setup form, every time.
    if buyer_email:
        try:
            from src.auth import send_magic_link, supabase_configured
            if supabase_configured():
                send_magic_link(buyer_email, f"{PUBLIC_BASE_URL}/dashboard/auth/callback")
        except Exception as exc:  # noqa: BLE001
            print(f"[orders/setup] send_magic_link failed for {buyer_email}: "
                  f"{type(exc).__name__}: {exc}")

    # Hand off to the existing fulfilment path.
    threading.Thread(target=_fulfil_order, args=(meta,), daemon=True).start()
    PENDING_ORDERS.pop(session_id, None)

    # Build the thank-you URL with everything the template needs to confirm
    # what was purchased + which inbox to check. Tier is the canonical key
    # ('two_engine', 'full_audit', etc.) — the template maps it to a label.
    from urllib.parse import urlencode
    qs = urlencode({
        "email": buyer_email,
        "brand": brand_name,
        "domain": domain,
        "tier": tier,
    })
    return RedirectResponse(f"/orders/thank-you?{qs}", status_code=303)


@app.get("/orders/thank-you", response_class=HTMLResponse)
def orders_thank_you(
    email: str = "",
    brand: str = "",
    domain: str = "",
    tier: str = "",
) -> HTMLResponse:
    """Post-purchase confirmation page. Lands the buyer here after they
    finish the competitors setup form. Tells them what's in motion (audit
    running, magic link sent), so the generic 'Welcome back' sign-in page
    doesn't feel like a dead end."""
    plan = TIER_PLANS.get(tier, {})
    raw_label = plan.get("label") or tier or "your audit"
    tier_label = raw_label.split(" (")[0] if " (" in raw_label else raw_label
    return HTMLResponse(_jinja.get_template("orders_thank_you.html.j2").render(
        email=email,
        brand=brand,
        domain=domain,
        tier=tier,
        tier_label=tier_label,
        is_subscription=(plan.get("stripe_mode") == "subscription"),
        dashboard_url=f"{PUBLIC_BASE_URL}/dashboard",
    ))


@app.get("/checkout/cancel")
def checkout_cancel(session_id: str | None = None) -> RedirectResponse:
    """Stripe sends the buyer here when they hit Back / Close on the
    Checkout page. We stashed return_to in the session metadata when we
    created the Checkout Session — read it back and bounce them home to
    the page they were on (the report, pricing, etc.)."""
    target = "/"
    if session_id:
        meta: dict[str, Any] = PENDING_ORDERS.get(session_id) or {}
        if not meta and stripe.api_key:
            try:
                sess_dict = _to_plain_dict(stripe.checkout.Session.retrieve(session_id))
                meta = sess_dict.get("metadata") or {}
            except Exception:  # noqa: BLE001
                meta = {}
        candidate = _safe_return_to((meta.get("return_to") or "").strip())
        if candidate:
            target = candidate
    return RedirectResponse(target, status_code=303)


def _record_purchase(
    *,
    email: str,
    tier: str,
    brand_name: str = "",
    domain: str = "",
    stripe_event_id: str = "",
    stripe_session_id: str = "",
    stripe_invoice_id: str = "",
    kind: str = "one_off",
    amount_usd: float | None = None,
) -> bool:
    """Persist a Purchase row idempotently. Skips if a row with the same
    stripe_event_id already exists, so re-delivered webhooks don't double-count.
    Pulls amount from TIER_PLANS when the caller doesn't pass an explicit one.
    Returns True if a new row was inserted, False if duplicate / skipped /
    failed — callers use this to gate one-shot side effects (welcome email
    etc.) so webhook redeliveries don't re-trigger them."""
    if not email or not tier:
        return False
    if amount_usd is None:
        amount_usd = float(TIER_PLANS.get(tier, {}).get("price_usd", 0))
    try:
        from sqlmodel import select as _select
        from src.db import Purchase, get_session
        with get_session() as s:
            if stripe_event_id:
                existing = s.exec(
                    _select(Purchase).where(Purchase.stripe_event_id == stripe_event_id)
                ).first()
                if existing:
                    return False
            row = Purchase(
                email=email.strip().lower(),
                tier=tier,
                amount_usd=amount_usd,
                brand_name=brand_name,
                domain=domain,
                stripe_event_id=stripe_event_id,
                stripe_session_id=stripe_session_id,
                stripe_invoice_id=stripe_invoice_id,
                kind=kind,
            )
            s.add(row)
            s.commit()
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[purchase] persist failed: {type(exc).__name__}: {exc}")
        return False


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event_obj = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise HTTPException(400, f"Webhook signature failed: {exc}")

    event = _to_plain_dict(event_obj)
    event_id = event.get("id") or ""

    if event.get("type") == "checkout.session.completed":
        session = (event.get("data") or {}).get("object") or {}
        sid = session.get("id") or ""
        # Fold Stripe-collected email into meta so the post-payment setup
        # page and _fulfil_order both have it (we don't pre-supply email
        # for /buy-driven flows).
        meta = _meta_with_email(session.get("metadata") or {}, session)
        # Don't fire the audit yet — the customer still needs to enter their
        # competitor list via /checkout/success → /orders/setup.
        PENDING_ORDERS[sid] = meta
        # Persist the purchase regardless of whether they finish the setup
        # form so the admin revenue counter never under-reports.
        tier = (meta.get("tier") or "").strip()
        plan = TIER_PLANS.get(tier, {})
        amount = (session.get("amount_total") or 0) / 100.0
        buyer_email = (session.get("customer_email") or meta.get("email") or "").strip()
        is_subscription = plan.get("stripe_mode") == "subscription"
        is_new = _record_purchase(
            email=buyer_email,
            tier=tier,
            brand_name=meta.get("brand_name") or "",
            domain=meta.get("domain") or "",
            stripe_event_id=event_id,
            stripe_session_id=sid,
            kind=("subscription_initial" if is_subscription else "one_off"),
            amount_usd=amount or float(plan.get("price_usd", 0)),
        )
        # Send the "thanks for signing up" welcome email once per purchase.
        # Gated on _record_purchase returning True so Stripe webhook
        # redeliveries don't re-send it. Wrapped in try/except so a Resend
        # outage can't make the webhook 500 and trigger retries.
        if is_new and buyer_email and tier:
            try:
                from src.delivery import send_welcome
                send_welcome(
                    to_email=buyer_email,
                    brand_name=(meta.get("brand_name") or "").strip(),
                    tier=tier,
                    is_subscription=is_subscription,
                    dashboard_url=f"{PUBLIC_BASE_URL}/dashboard",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[welcome-email] send failed for {buyer_email}: "
                      f"{type(exc).__name__}: {exc}")

    elif event.get("type") == "invoice.paid":
        # Subscription renewals — Stripe sends one of these per billing cycle
        # after the initial checkout. Skip the very first invoice (already
        # captured by checkout.session.completed) by checking billing_reason.
        invoice = (event.get("data") or {}).get("object") or {}
        reason = invoice.get("billing_reason") or ""
        if reason in ("subscription_create",):
            # Initial sub charge — already recorded via checkout.session.completed.
            return JSONResponse({"received": True})
        amount = (invoice.get("amount_paid") or 0) / 100.0
        if amount <= 0:
            return JSONResponse({"received": True})
        # Resolve tier from the line item's price id.
        line_items = (invoice.get("lines") or {}).get("data") or []
        price_id = ""
        for item in line_items:
            price_id = (item.get("price") or {}).get("id") or ""
            if price_id:
                break
        tier = ""
        for t, plan in TIER_PLANS.items():
            if os.environ.get(plan.get("stripe_env", ""), "").strip() == price_id:
                tier = t
                break
        _record_purchase(
            email=(invoice.get("customer_email") or "").strip(),
            tier=tier,
            stripe_event_id=event_id,
            stripe_invoice_id=invoice.get("id") or "",
            kind="subscription_renewal",
            amount_usd=amount,
        )

    return JSONResponse({"received": True})


RUN_ANOTHER_BAR = """
<div class="report-floating-cta" style="position: fixed; bottom: 22px; right: 22px; z-index: 9999;
            font-family: Inter, system-ui, sans-serif;">
  <a href="/" style="display: inline-flex; align-items: center; gap: 8px;
                     padding: 12px 18px; border-radius: 999px;
                     background: linear-gradient(135deg, #2563eb, #7c3aed);
                     color: white; text-decoration: none; font-weight: 800;
                     font-size: 13px; letter-spacing: -.01em;
                     box-shadow: 0 18px 38px rgba(37,99,235,.34);">
    + Run another preview
  </a>
</div>
"""

# Injected into every served report. Detects whether the page is inside an
# iframe (i.e. wrapped by /dashboard/reports/{id}) and hides the in-report
# horizontal nav + floating "Run another preview" CTA — both are redundant
# when the dashboard chrome and side-nav are already present. Standalone
# (full-screen) views keep them.
EMBED_AWARE_HEAD = """
<meta name="robots" content="noindex,nofollow,noarchive,nosnippet">
<style>
  html.embedded .report-nav,
  html.embedded nav.report-nav,
  html.embedded .report-floating-cta,
  html.embedded .report-claim-bar,
  html.embedded .save-form { display: none !important; }
  /* When the report is in the dashboard the user is already signed in and
     the report is already saved — swap the free-tier card's save form for a
     confirmation pill. */
  html.embedded .price-card.current::after {
    content: "✓ Saved to your dashboard";
    display: block; margin-top: auto; padding: 12px 18px;
    border-radius: 999px; background: rgba(16,185,129,.18);
    color: #6ee7b7; font-weight: 800; font-size: 13px; text-align: center;
    border: 1px solid rgba(16,185,129,.30);
  }
</style>
<script>
  (function () {
    try {
      if (window.self !== window.top) {
        document.documentElement.classList.add('embedded');
      }
    } catch (e) {
      document.documentElement.classList.add('embedded');
    }
  })();
</script>
"""


def _claim_cta_for(run_id: str) -> str:
    """Sticky top bar on the public report inviting the visitor to keep this
    report in their dashboard by signing up. Hidden when the report is
    embedded (handled by the .embedded CSS rule above)."""
    return f"""
<div class="report-claim-bar" style="position: sticky; top: 0; z-index: 9998;
            display: flex; align-items: center; justify-content: center; gap: 14px;
            padding: 11px 18px;
            background: linear-gradient(135deg, rgba(37,99,235,.96), rgba(124,58,237,.94));
            color: white; font-family: Inter, system-ui, sans-serif;
            font-size: 13.5px; font-weight: 700; box-shadow: 0 8px 22px rgba(37,99,235,.22);">
  <span>Save this report to your dashboard — sign up free.</span>
  <a href="/dashboard/login?claim={run_id}"
     style="display: inline-flex; align-items: center; gap: 6px;
            padding: 8px 16px; border-radius: 999px;
            background: white; color: #1d4ed8; text-decoration: none;
            font-weight: 800; font-size: 13px;">
    Save &amp; sign up →
  </a>
</div>
"""


def _rerender_from_cache(run_id: str, run_dir: Path, tier: str) -> str:
    """Re-render report HTML from cached raw_responses.json using the current
    template — no API calls. LLM scores and action plan are not cached, so they
    come back as None (templates handle missing values)."""
    import json as _json
    from src.models import EngineResponse
    raw_path = run_dir / "raw_responses.json"
    if not raw_path.exists():
        raise HTTPException(404, "raw_responses.json not found — cannot re-render")
    raw = _json.loads(raw_path.read_text())
    responses = [EngineResponse.model_validate(r) for r in raw]

    # Reconstruct site config: brand name from CLI'd config, but override domain
    # from any cited own-domain we can find. Falls back to default site.yaml.
    site = SiteConfig.model_validate(yaml.safe_load(DEFAULT_CONFIG_PATH.open()))

    # Re-run free tech audit (HTTP only, no spend)
    try:
        tech = asyncio.run(run_tech_audit_async(site.brand.domain))
    except Exception:
        tech = None

    rows = [
        ScoredRow(response=r, deterministic=score_response(r, site), llm=None)
        for r in responses
    ]

    screenshot_file = run_dir / "site_screenshot.png"
    write_html(
        rows,
        site,
        run_dir,
        tier=tier,
        screenshot=screenshot_file.name if screenshot_file.exists() else None,
        action_plan=None,
        tech=tech,
    )
    return (run_dir / "report.html").read_text()


@app.get("/report/{run_id}", response_class=HTMLResponse)
def serve_report(run_id: str, refresh: int = 0, tier: str = "full") -> HTMLResponse:
    run_dir = OUTPUT_ROOT / run_id
    html_path = run_dir / "report.html"
    if not html_path.exists() and not refresh:
        raise HTTPException(404, "Report not found")
    if refresh:
        html = _rerender_from_cache(run_id, run_dir, tier=tier)
    else:
        html = html_path.read_text()
    # Inject <base> so relative asset paths (site_screenshot.png) resolve to
    # /report/{run_id}/… instead of /report/…, plus the embed-aware CSS that
    # hides the in-report nav when this page is loaded inside an iframe.
    head_inject = f'<base href="/report/{run_id}/">{EMBED_AWARE_HEAD}'
    if "<head>" in html:
        html = html.replace("<head>", f"<head>{head_inject}", 1)
    # Inject a floating "Run another preview" CTA when served over HTTP, so
    # visitors can audit a new domain without backing out manually. Hidden
    # when the report is embedded in /dashboard/reports/{id}.
    if "<body>" in html:
        html = html.replace("<body>", f"<body>{RUN_ANOTHER_BAR}", 1)
    return HTMLResponse(html)


@app.get("/report/{run_id}/site_screenshot.png")
def serve_screenshot(run_id: str):
    from fastapi.responses import FileResponse
    p = OUTPUT_ROOT / run_id / "site_screenshot.png"
    if not p.exists():
        raise HTTPException(404, "Screenshot not found")
    return FileResponse(p, media_type="image/png")


@app.get("/report/{run_id}/pdf")
def serve_pdf(run_id: str) -> RedirectResponse:
    run_dir = OUTPUT_ROOT / run_id
    pdf_path = run_dir / "report.pdf"
    if not pdf_path.exists():
        raise HTTPException(404, "PDF not found")
    return RedirectResponse(f"/static/{run_id}/report.pdf")


def _build_site_for_order(meta: dict[str, Any]) -> SiteConfig:
    """For now, use the on-disk config as a base and override brand name, domain
    and competitor list from the Stripe metadata + setup form. Production would
    pull from a per-customer DB row."""
    base = SiteConfig.model_validate(yaml.safe_load(DEFAULT_CONFIG_PATH.open()))
    base.brand.name = meta.get("brand_name") or base.brand.name
    base.brand.domain = meta.get("domain") or base.brand.domain
    competitors = meta.get("competitors")
    if isinstance(competitors, list):
        # Customer-supplied list takes precedence — replace the on-disk default
        # so we never bleed an unrelated brand's competitors into this report.
        base.competitors = competitors
        base.ground_truth = []
    return base


def _fulfil_order(meta: dict[str, Any]) -> None:
    """Outer wrapper that catches anything unexpected so a background-thread
    crash can't silently orphan a paying customer. Logs visibly and still
    attempts the dashboard hydration with whatever data we have so the user
    at least lands in /dashboard (vs an empty void)."""
    try:
        _fulfil_order_inner(meta)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[fulfil] UNCAUGHT in _fulfil_order for {meta.get('email')!r}: "
              f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        # Last-ditch hydration: try to at least give the customer a dashboard
        # row so they can see SOMETHING. Audit data may be partial.
        try:
            email = (meta.get("email") or "").strip()
            tier = (meta.get("tier") or "").strip()
            if email and tier in TIER_PLANS:
                site = _build_site_for_order(meta)
                _ensure_dashboard_for_paid_order(
                    email=email, tier=tier, site=site,
                    run_id=_make_run_id(site.brand.name), rows=[],
                )
        except Exception as inner:  # noqa: BLE001
            print(f"[fulfil] last-ditch hydration also failed: {type(inner).__name__}: {inner}")


def _fulfil_order_inner(meta: dict[str, Any]) -> None:
    """Run the audit, generate PDF, email the customer.
    All errors are swallowed and logged — the customer record gets retried via Stripe.

    Monthly tiers run the same first audit as their one-off counterpart so the
    customer gets data on day one. Future scheduled re-runs come from a
    separate cron path, not from this fulfilment hook."""
    tier = (meta.get("tier") or "").strip()
    if tier not in TIER_PLANS:
        # Silent return here was hiding a real bug — paid orders were dropping
        # because tier wasn't matching, but we had no signal in the logs. Log
        # the full meta so we can see exactly what Stripe handed us.
        print(
            f"[fulfil] BAIL: tier {tier!r} not in TIER_PLANS "
            f"(valid={sorted(TIER_PLANS)}). meta={meta!r}"
        )
        return
    plan = TIER_PLANS[tier]
    email = meta.get("email")
    if not email:
        print(f"[fulfil] BAIL: no email on tier={tier!r} meta={meta!r}")
        return
    print(f"[fulfil] starting audit for {email!r} tier={tier!r} brand={meta.get('brand_name')!r}")

    # Publish the 'Starting up…' banner FIRST so the customer sees progress
    # even if the next steps hang. Without this, the audit could spend 30s+
    # building site config / capturing screenshot / etc. while the dashboard
    # shows 'No audits run yet' — looking broken even though it's working.
    _publish_paid_status(
        email, "Starting your audit…", 5,
        brand=meta.get("brand_name") or "", domain=meta.get("domain") or "",
        tier=tier,
        started_at=datetime.utcnow().isoformat(),
    )

    # Per-step timing prints so the next stalled audit reveals exactly which
    # call hangs. Remove once we've pinned down the regression.
    import time as _time
    t0 = _time.monotonic()
    print(f"[fulfil] step=build_site start email={email!r}")
    site = _build_site_for_order(meta)
    print(f"[fulfil] step=build_site done in {_time.monotonic()-t0:.2f}s")

    t0 = _time.monotonic()
    print(f"[fulfil] step=generate_queries start")
    # Generate brand-aware queries from the customer's actual brand name +
    # competitor list. The old config/queries.csv path was hardcoded to a
    # single seed brand, so every paid audit was effectively asking the
    # engines about that brand instead of the customer's — producing
    # hallucination flags on every row.
    queries = _generate_paid_queries(site.brand.name, list(site.competitors or []))
    print(f"[fulfil] step=generate_queries done in {_time.monotonic()-t0:.2f}s ({len(queries)} queries)")

    # Filter engines per the tier's plan
    plan_engines = plan["engines"]
    only_labels = None if plan_engines == "all" else set(plan_engines)

    engine_objs = []
    for cfg in site.engines.openrouter:
        if only_labels and cfg.label not in only_labels:
            continue
        engine_objs.append(OpenRouterEngine(model=cfg.model, label=cfg.label))
    for cfg in site.engines.apify:
        if only_labels and cfg.label not in only_labels:
            continue
        engine_objs.append(
            ApifyEngine(
                label=cfg.label,
                country_code=site.locale.country,
                language_code=site.locale.language,
            )
        )
    print(f"[fulfil] step=engines_resolved count={len(engine_objs)} labels={[e.label for e in engine_objs]}")

    run_id = _make_run_id(site.brand.name)
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"[fulfil] step=run_dir_created run_id={run_id!r} path={run_dir!s}")

    t0 = _time.monotonic()
    print(f"[fulfil] step=screenshot start domain={site.brand.domain!r}")
    screenshot_path = capture_screenshot(site.brand.domain, run_dir)
    print(f"[fulfil] step=screenshot done in {_time.monotonic()-t0:.2f}s path={screenshot_path!s}")
    _publish_paid_status(email, f"Asking {len(engine_objs)} AI engine{'s' if len(engine_objs) != 1 else ''} {len(queries)} buyer questions…", 15)

    async def _gather():
        return await asyncio.gather(
            run_audit(engine_objs, queries, run_dir),
            run_tech_audit_async(site.brand.domain),
        )

    responses, tech = asyncio.run(_gather())
    _publish_paid_status(email, "Identifying competitors named in the answers…", 55)

    # LLM scoring is best-effort. A network blip or OpenRouter timeout
    # MUST NOT abort the whole fulfilment — the customer has paid and we
    # owe them the deterministic report + dashboard access even if the
    # second-pass scoring degrades to None.
    if plan["llm_scoring"]:
        _publish_paid_status(email, "Scoring sentiment, accuracy and hallucination flags…", 65)
        try:
            llm_scores: list[LLMScore | None] = list(asyncio.run(score_all(responses, site)))
        except Exception as exc:  # noqa: BLE001
            print(f"[fulfil] llm scoring failed: {type(exc).__name__}: {exc}")
            llm_scores = [None] * len(responses)
    else:
        llm_scores = [None] * len(responses)

    rows = [
        ScoredRow(
            response=r,
            deterministic=score_response(r, site),
            llm=llm,
        )
        for r, llm in zip(responses, llm_scores)
    ]

    # Same story for the action plan — Sonnet 4.6 occasionally times out
    # or rate-limits. Don't kill the order over it.
    action_plan = None
    if plan["action_plan"]:
        _publish_paid_status(email, "Generating your prioritised action plan with Claude…", 80)
        try:
            action_plan = generate_action_plan(rows, site)
        except Exception as exc:  # noqa: BLE001
            print(f"[fulfil] action_plan generation failed: {type(exc).__name__}: {exc}")
            action_plan = None

    _publish_paid_status(email, "Assembling your report…", 92)
    write_csv(rows, run_dir)
    write_html(
        rows,
        site,
        run_dir,
        tier=tier,
        screenshot=screenshot_path.name if screenshot_path else None,
        action_plan=action_plan,
        tech=tech,
    )
    # Hero image embedded inline at the top of the delivery email.
    # Reuses the same composer the cold-email teaser uses; non-fatal on failure.
    hero_path: Path | None = run_dir / "email_hero.png"
    try:
        from src.teaser_image import generate as generate_teaser
        visibility_pct = sum(1 for r in rows if r.deterministic.mentioned) / max(1, len(rows)) * 100
        competitor_names: list[str] = []
        for r in rows:
            for c in r.deterministic.competitors_mentioned:
                if c not in competitor_names:
                    competitor_names.append(c)
        generate_teaser(
            brand_name=site.brand.name,
            domain=site.brand.domain,
            visibility_pct=visibility_pct,
            competitors=competitor_names,
            site_screenshot=screenshot_path if screenshot_path else None,
            output_path=hero_path,
        )
    except Exception:  # noqa: BLE001
        hero_path = None

    dashboard_url = f"{PUBLIC_BASE_URL}/dashboard"
    try:
        send_report(
            to_email=email,
            brand_name=site.brand.name,
            tier=tier,
            dashboard_url=dashboard_url,
            hero_image_path=hero_path,
        )
    except Exception as exc:  # noqa: BLE001
        # Persist a tombstone so we can retry/inspect manually.
        (run_dir / "delivery_error.log").write_text(
            f"{type(exc).__name__}: {exc}\n  to: {email}\n"
        )

    # Auto-hydrate the customer's dashboard: ensure they have a Supabase
    # auth user, create a TrackedBrand pointing at this audit, persist the
    # AuditRunRecord, and trigger a magic-link sign-in email. After they
    # click it they land in /dashboard with the brand + report already
    # present — not an empty workspace.
    # Pass the generated query strings through so the brand row's
    # monitored_queries gets seeded — keeps subsequent dashboard re-runs
    # consistent with the first paid audit (same 40 questions every time).
    _ensure_dashboard_for_paid_order(
        email=email,
        tier=tier,
        site=site,
        run_id=run_id,
        rows=rows,
        monitored_queries=[q.query for q in queries],
    )

    # Mark the audit as complete so the dashboard's pending-banner polling
    # knows to reload the page and show the new brand + report.
    key = email.strip().lower() if email else ""
    if key:
        job = PAID_AUDIT_JOBS.get(key, {})
        job["status"] = "complete"
        job["step"] = "Done — refreshing your dashboard…"
        job["pct"] = 100
        PAID_AUDIT_JOBS[key] = job


def _ensure_dashboard_for_paid_order(
    *,
    email: str,
    tier: str,
    site: SiteConfig,
    run_id: str,
    rows: list[ScoredRow],
    monitored_queries: list[str] | None = None,
) -> None:
    """Auto-create the customer's dashboard footprint after a paid order.
    Looks up or creates the Supabase auth user, creates a TrackedBrand
    (re-using an existing one for the same user+domain), inserts an
    AuditRunRecord pointing at the just-completed run, then sends a
    magic-link so they can sign in immediately. Fail-soft — any error
    is logged and the email/report still went out."""
    if not email:
        return
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    if not service_key or not supabase_url:
        print("[fulfil] SUPABASE_SERVICE_ROLE_KEY not set — skipping dashboard hydration")
        return
    try:
        from supabase import create_client  # type: ignore[import-not-found]
        from src.db import get_session, TrackedBrand, AuditRunRecord
        from sqlmodel import select as _select
        from uuid import UUID as _UUID
        admin = create_client(supabase_url, service_key)
        email_lower = email.strip().lower()

        # 1. Resolve / create the Supabase auth user.
        user_id: str | None = None
        try:
            created = admin.auth.admin.create_user({
                "email": email_lower,
                "email_confirm": True,
            })
            if created and getattr(created, "user", None):
                user_id = created.user.id
        except Exception:  # noqa: BLE001 — usually "User already registered"
            try:
                page = 1
                while user_id is None:
                    listed = admin.auth.admin.list_users(page=page, per_page=200)
                    items = getattr(listed, "users", None) or listed or []
                    if not items:
                        break
                    for u in items:
                        u_email = getattr(u, "email", "") or ""
                        if u_email.lower() == email_lower:
                            user_id = u.id
                            break
                    if len(items) < 200:
                        break
                    page += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[fulfil] list_users failed: {type(exc).__name__}: {exc}")
        if not user_id:
            print(f"[fulfil] couldn't resolve Supabase user_id for {email_lower}")
            return

        # 2. Find / create the TrackedBrand. Monthly tiers seed the
        #    scheduled-run cron; one-offs leave tier='' so cron doesn't
        #    pick them up.
        is_monthly = tier in ("two_engine_monthly", "full_monthly")
        with get_session() as s:
            existing_brand = s.exec(
                _select(TrackedBrand)
                .where(TrackedBrand.user_id == _UUID(user_id))
                .where(TrackedBrand.domain == site.brand.domain)
            ).first()
            if existing_brand:
                # Upgrade tier if customer just bought a recurring plan and
                # the brand was previously free or one-off.
                if is_monthly and not existing_brand.tier:
                    existing_brand.tier = tier
                    from src.dashboard import _compute_next_scheduled_run
                    existing_brand.next_scheduled_run = _compute_next_scheduled_run()
                # Seed monitored_queries once so future dashboard re-runs
                # use the same brand-aware 40 questions, not the 8-query
                # fallback that kicks in on empty monitored_queries.
                if monitored_queries and not existing_brand.monitored_queries:
                    existing_brand.monitored_queries = list(monitored_queries)
                s.add(existing_brand)
                s.commit()
                brand_id = existing_brand.id
            else:
                from src.dashboard import DEFAULT_ENGINES, _compute_next_scheduled_run
                competitors_list = [
                    {"name": c, "domain": ""}
                    for c in (site.competitors or [])
                    if c
                ]
                new_brand = TrackedBrand(
                    user_id=_UUID(user_id),
                    name=site.brand.name,
                    domain=site.brand.domain,
                    aliases=list(site.brand.aliases or []),
                    competitors=competitors_list,
                    ground_truth=list(site.ground_truth or []),
                    engines=list(DEFAULT_ENGINES),
                    monitored_queries=list(monitored_queries or []),
                    locale_country=(site.locale.country or "US").upper(),
                    locale_language=site.locale.language or "en",
                    tier=tier if is_monthly else "",
                    next_scheduled_run=_compute_next_scheduled_run() if is_monthly else None,
                )
                s.add(new_brand)
                s.commit()
                s.refresh(new_brand)
                brand_id = new_brand.id

            # 3. Insert the AuditRunRecord pointing at the just-completed run.
            existing_run = s.exec(
                _select(AuditRunRecord).where(AuditRunRecord.run_id == run_id)
            ).first()
            if not existing_run:
                n = max(1, len(rows))
                visibility = sum(1 for r in rows if r.deterministic.mentioned) / n
                citation = sum(1 for r in rows if r.deterministic.cited_as_source) / n
                sov: dict[str, int] = {}
                for r in rows:
                    for c in r.deterministic.competitors_mentioned:
                        sov[c] = sov.get(c, 0) + 1
                run_rec = AuditRunRecord(
                    brand_id=brand_id,
                    user_id=_UUID(user_id),
                    run_id=run_id,
                    status="complete",
                    finished_at=datetime.utcnow(),
                    queries_total=n,
                    visibility_rate=visibility,
                    citation_rate=citation,
                    share_of_voice=sov,
                )
                s.add(run_rec)
                s.commit()

        # Magic-link sign-in email is now sent up-front in /orders/setup
        # (independent of audit success), so nothing to do here.

    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[fulfil] dashboard hydration failed for {email}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
