"""FastAPI server: Stripe Checkout + webhook → triggers an audit run, generates
PDF, emails the customer.

Run locally:
    uvicorn src.server:app --reload --port 8000

Stripe webhook for local dev:
    stripe listen --forward-to localhost:8000/webhooks/stripe
"""
from __future__ import annotations

import asyncio
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
        "action_plan": False,
        "monitored_query_limit": 0,  # one-shot tier — n/a
    },
    "full_audit": {
        "label": "Full Audit ($79)",
        "price_usd": 79,
        "stripe_mode": "payment",
        "stripe_env": "STRIPE_PRICE_FULL_AUDIT",
        "engines": "all",
        "llm_scoring": True,
        "action_plan": False,
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
    )
    return PlainTextResponse(body, media_type="text/plain")


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
    return _render("pricing.html.j2", request=request)


@app.get("/what-is-aeo", response_class=HTMLResponse)
def page_what_is_aeo(request: Request) -> HTMLResponse:
    return _render("what_is_aeo.html.j2", request=request)


@app.get("/aeo-vs-seo", response_class=HTMLResponse)
def page_aeo_vs_seo(request: Request) -> HTMLResponse:
    return _render("aeo_vs_seo.html.j2", request=request)


@app.get("/what-is-geo", response_class=HTMLResponse)
def page_what_is_geo(request: Request) -> HTMLResponse:
    return _render("what_is_geo.html.j2", request=request)


@app.get("/product/audit", response_class=HTMLResponse)
def page_product_audit(request: Request) -> HTMLResponse:
    return _render("product_audit.html.j2", request=request)


@app.get("/product/monitoring", response_class=HTMLResponse)
def page_product_monitoring(request: Request) -> HTMLResponse:
    return _render("product_monitoring.html.j2", request=request)


@app.get("/how-it-works", response_class=HTMLResponse)
def page_how_it_works(request: Request) -> HTMLResponse:
    return _render("how_it_works.html.j2", request=request)


@app.get("/privacy", response_class=HTMLResponse)
def page_privacy(request: Request) -> HTMLResponse:
    return _render("privacy.html.j2", request=request)


@app.get("/terms", response_class=HTMLResponse)
def page_terms(request: Request) -> HTMLResponse:
    return _render("terms.html.j2", request=request)


@app.get("/support", response_class=HTMLResponse)
def page_support(request: Request, status: str = "") -> HTMLResponse:
    return _render("support.html.j2", request=request, status=status or None)


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


def _run_preview_job(
    run_id: str, domain: str, brand_name: str, category: str | None, country: str = "US"
) -> None:
    """Background worker for a free preview. Updates PREVIEW_JOBS as it progresses."""
    _set_step(run_id, "Capturing site screenshot…", 8)
    try:
        site = _build_preview_site(domain, brand_name, country=country)
        queries = _generic_free_queries(brand_name, category)
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

        _set_step(run_id, f"Asking Google AI {len(queries)} buyer-facing questions…", 25)

        async def _gather():
            return await asyncio.gather(
                run_audit(engine_objs, queries, run_dir),
                run_tech_audit_async(domain),
            )

        responses, tech = asyncio.run(_gather())
        _set_step(run_id, "Extracting competitors from the answers…", 70)

        # Auto-extract competitors from the responses via Haiku, then inject
        # into the SiteConfig so the deterministic scorer can flag them in the
        # text + citations. ~$0.001 per preview, non-fatal on failure.
        try:
            site.competitors = asyncio.run(
                extract_competitors(responses, brand_name)
            )
        except Exception:  # noqa: BLE001
            site.competitors = []

        _set_step(run_id, "Scoring visibility and citations…", 85)
        rows = [
            ScoredRow(
                response=r,
                deterministic=score_response(r, site),
                llm=LLMScore(),
            )
            for r in responses
        ]
        write_csv(rows, run_dir)
        _set_step(run_id, "Rendering your report…", 95)
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


def _start_preview(
    domain: str, brand: str, category: str | None, country: str | None = None
) -> tuple[str, str, str, str]:
    """Validate inputs, kick off a background preview run.
    Returns (run_id, normalised_domain, brand, resolved_country).
    Raises HTTPException(400) on bad input."""
    norm = _normalise_domain(domain)
    if not norm or "." not in norm:
        raise HTTPException(400, "Please enter a valid domain (e.g. capify.com.au)")
    brand = (brand or "").strip()
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
    """Cold-email entry point. Either short (`d`/`b`/`c`/`co`) or long
    (`domain`/`brand`/`category`/`country`) query params work — short keeps
    email URLs compact."""
    run_id, norm, real_brand, resolved_country = _start_preview(
        d or domain, b or brand, c or category, co or country,
    )
    html = _jinja.get_template("loading.html.j2").render(
        run_id=run_id,
        brand_name=real_brand,
        domain=norm,
        country_code=resolved_country,
        country_name=SUPPORTED_COUNTRIES.get(resolved_country, resolved_country),
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

class TeaserRequest(BaseModel):
    domain: str
    brand: str
    category: str | None = None
    # When true, delete the cached site screenshot + teaser image before
    # regenerating. Use this after deploying a teaser-design change or
    # after rotating SCREENSHOTAPI_TOKEN. Idempotent + safe.
    force: bool = False


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


@app.post("/api/teaser")
def api_teaser(req: TeaserRequest) -> JSONResponse:
    """Cold-email integration point. Runs ONE Apify query, extracts competitors
    via Haiku, generates a hero image, and returns the asset URLs your email
    sender can drop straight into the message body.

    Cost: ~$0.0085 per call (1 Apify SERP + 1 Haiku extraction).
    Returns: {teaser_image_url, click_url, brand_name, domain, visibility_pct, competitors}
    """
    import hashlib
    from urllib.parse import urlencode

    norm = _normalise_domain(req.domain)
    if not norm or "." not in norm:
        raise HTTPException(400, "Invalid domain")
    brand = (req.brand or "").strip()
    if not brand:
        raise HTTPException(400, "brand is required")
    category = (req.category or "").strip()

    # Single Apify query — "best {category}" surfaces competitors most directly.
    # Falls back to "What is {brand}?" when no category is supplied.
    teaser_query = f"best {category}" if category else f"What is {brand}?"
    query_type = "category" if category else "brand"

    apify = ApifyEngine(
        label=FREE_TIER_ENGINE,
        country_code="US",
        language_code="en",
    )
    response = asyncio.run(apify.query(teaser_query, query_type))

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

    # Compose the teaser image
    from src.teaser_image import generate as generate_teaser
    img_hash = hashlib.md5(
        f"{norm}|{brand}|{visibility_pct}|{','.join(competitors[:3])}".encode()
    ).hexdigest()[:16]
    img_filename = f"{img_hash}.png"
    img_path = teasers_dir / img_filename
    # generate_teaser always overwrites, so disk content is always fresh.
    # We only need to delete the file under force=True if we wanted to ensure
    # next-tick consistency — but the overwrite is atomic on the same FS so
    # we just regenerate unconditionally.
    try:
        generate_teaser(
            brand_name=brand,
            domain=norm,
            visibility_pct=visibility_pct,
            competitors=competitors,
            site_screenshot=site_screenshot if (site_screenshot and site_screenshot.exists()) else None,
            output_path=img_path,
            category=category or None,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Teaser image generation failed: {exc}")

    # mtime-based cache-buster so email clients + browsers fetch the new
    # image after each regen, even though the path is content-hashed.
    try:
        ver = int(img_path.stat().st_mtime)
    except OSError:
        ver = 0
    click_qs = urlencode({"d": norm, "b": brand, "c": category} if category else {"d": norm, "b": brand})
    return JSONResponse({
        "brand_name": brand,
        "domain": norm,
        "visibility_pct": visibility_pct,
        "competitors": competitors,
        "teaser_image_url": f"{SITE_BASE_URL}/teasers/{img_filename}?v={ver}",
        "click_url": f"{SITE_BASE_URL}/preview?{click_qs}",
        "teaser_query": teaser_query,
        "answered_in": ["text" if brand_in_text else None, "citations" if brand_in_citations else None],
    })


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
        success_url=f"{PUBLIC_BASE_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_BASE_URL}/checkout/cancel",
        metadata={
            "tier": req.tier,
            "brand_name": req.brand_name,
            "domain": req.domain,
            "email": req.email,
        },
    )
    return JSONResponse({"id": session.id, "url": session.url})


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
            sess = stripe.checkout.Session.retrieve(session_id)
            meta = sess.get("metadata") or {}
            PENDING_ORDERS[session_id] = meta
        except Exception:  # noqa: BLE001
            meta = {}

    brand = meta.get("brand_name") or "your brand"
    domain = meta.get("domain") or ""
    tier = meta.get("tier") or ""
    sid = session_id or ""
    return HTMLResponse(_jinja.get_template("checkout_setup.html.j2").render(
        brand=brand, domain=domain, tier=tier, session_id=sid,
    ))


@app.post("/orders/setup")
def orders_setup(
    session_id: str = Form(...),
    competitor_1: str = Form(""),
    competitor_2: str = Form(""),
    competitor_3: str = Form(""),
    competitor_4: str = Form(""),
    competitor_5: str = Form(""),
) -> HTMLResponse:
    """Receive the post-payment setup form, attach competitors to the order
    metadata, and fire the audit in the background."""
    meta = PENDING_ORDERS.get(session_id)
    if not meta:
        if not session_id or not stripe.api_key:
            raise HTTPException(404, "Unknown session")
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            meta = sess.get("metadata") or {}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, f"Could not load session: {exc}")

    competitors = [
        c.strip() for c in (competitor_1, competitor_2, competitor_3, competitor_4, competitor_5)
        if c and c.strip()
    ]
    meta = dict(meta)  # don't mutate the registry entry directly
    meta["competitors"] = competitors

    # Hand off to the existing fulfilment path.
    threading.Thread(target=_fulfil_order, args=(meta,), daemon=True).start()
    PENDING_ORDERS.pop(session_id, None)

    brand = meta.get("brand_name") or "your brand"
    return HTMLResponse(f"""
    <div style="font-family: system-ui; padding: 60px; text-align: center; max-width: 560px; margin: 0 auto;">
      <h1>Audit started ✓</h1>
      <p>We're auditing <strong>{brand}</strong> across the AI engines now. You'll get an email
      with the full report and PDF in a few minutes.</p>
      <p style="color: #64748b; font-size: 14px; margin-top: 24px;">
        Tracking against {len(competitors)} competitor{'s' if len(competitors) != 1 else ''}:
        {', '.join(competitors) if competitors else 'none specified'}
      </p>
    </div>
    """)


@app.get("/checkout/cancel", response_class=HTMLResponse)
def checkout_cancel() -> str:
    return "<p>Checkout cancelled.</p>"


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise HTTPException(400, f"Webhook signature failed: {exc}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        sid = session.get("id") or ""
        meta = session.get("metadata") or {}
        # Don't fire the audit yet — the customer still needs to enter their
        # competitor list via /checkout/success → /orders/setup.
        PENDING_ORDERS[sid] = meta

    return JSONResponse({"received": True})


RUN_ANOTHER_BAR = """
<div style="position: fixed; bottom: 22px; right: 22px; z-index: 9999;
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
    # /report/{run_id}/… instead of /report/…
    base_tag = f'<base href="/report/{run_id}/">'
    if "<head>" in html:
        html = html.replace("<head>", f"<head>{base_tag}", 1)
    # Inject a floating "Run another preview" CTA when served over HTTP, so
    # visitors can audit a new domain without backing out manually.
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
    """Run the audit, generate PDF, email the customer.
    All errors are swallowed and logged — the customer record gets retried via Stripe.

    Monthly tiers run the same first audit as their one-off counterpart so the
    customer gets data on day one. Future scheduled re-runs come from a
    separate cron path, not from this fulfilment hook."""
    tier = (meta.get("tier") or "").strip()
    if tier not in TIER_PLANS:
        return
    plan = TIER_PLANS[tier]
    email = meta.get("email")
    if not email:
        return

    site = _build_site_for_order(meta)
    queries = _load_queries(DEFAULT_QUERIES_PATH)

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

    run_id = _make_run_id(site.brand.name)
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = capture_screenshot(site.brand.domain, run_dir)

    async def _gather():
        return await asyncio.gather(
            run_audit(engine_objs, queries, run_dir),
            run_tech_audit_async(site.brand.domain),
        )

    responses, tech = asyncio.run(_gather())

    if plan["llm_scoring"]:
        llm_scores: list[LLMScore | None] = list(asyncio.run(score_all(responses, site)))
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

    action_plan = generate_action_plan(rows, site) if plan["action_plan"] else None

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
    pdf_path: Path | None = None
    try:
        from src.pdf import render as render_pdf
        pdf_path = render_pdf(run_dir)
    except Exception:  # noqa: BLE001
        pdf_path = None

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

    report_url = f"{PUBLIC_BASE_URL}/report/{run_id}"
    try:
        send_report(
            to_email=email,
            brand_name=site.brand.name,
            tier=tier,
            report_url=report_url,
            pdf_path=pdf_path,
            hero_image_path=hero_path,
        )
    except Exception as exc:  # noqa: BLE001
        # Persist a tombstone so we can retry/inspect manually.
        (run_dir / "delivery_error.log").write_text(
            f"{type(exc).__name__}: {exc}\n  to: {email}\n"
        )
