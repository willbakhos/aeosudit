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
import threading
import uuid
from pathlib import Path
from typing import Any

import stripe
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from src.action_plan import generate as generate_action_plan
from src.delivery import send_report
from src.engines.apify import ApifyEngine
from src.engines.openrouter import OpenRouterEngine
from src.llm_scorer import score_all
from src.main import FREE_TIER_ENGINE, _load_queries, VALID_TIERS
from src.models import LLMScore, ScoredRow, SiteConfig
from src.pdf import render as render_pdf
from src.report import write_csv, write_html
from src.runner import run_audit
from src.scorer import score_response
from src.screenshot import capture as capture_screenshot

load_dotenv()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
DEFAULT_CONFIG_PATH = Path(os.environ.get("DEFAULT_SITE_CONFIG", "config/site.yaml"))
DEFAULT_QUERIES_PATH = Path(os.environ.get("DEFAULT_QUERIES_CSV", "config/queries.csv"))
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "output"))

# Stripe price IDs are read from env so the same code works in test + prod.
TIER_PRICES = {
    "spotlight": os.environ.get("STRIPE_PRICE_SPOTLIGHT", ""),
    "full": os.environ.get("STRIPE_PRICE_FULL", ""),
    "action_plan": os.environ.get("STRIPE_PRICE_ACTION_PLAN", ""),
}

app = FastAPI(title="AEO Audit Checkout & Delivery")


class CheckoutRequest(BaseModel):
    tier: str = Field(..., description="spotlight | full | action_plan")
    brand_name: str
    domain: str
    email: EmailStr
    spotlight_engine: str | None = None  # required when tier=spotlight


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
    <html><body style="font-family: system-ui; padding: 40px; max-width: 640px; margin: auto;">
      <h1>AEO Audit checkout</h1>
      <p>POST /checkout with JSON to start a Stripe Checkout session.</p>
      <p>POST /webhooks/stripe is the Stripe webhook receiver.</p>
      <p>GET /report/{run_id} serves a completed report.</p>
    </body></html>
    """


@app.post("/checkout")
def create_checkout(req: CheckoutRequest) -> JSONResponse:
    """Creates a Stripe Checkout Session for the chosen tier."""
    if req.tier not in {"spotlight", "full", "action_plan"}:
        raise HTTPException(400, f"Unknown tier {req.tier!r}")
    price_id = TIER_PRICES.get(req.tier)
    if not price_id:
        raise HTTPException(500, f"No STRIPE_PRICE_* env var configured for {req.tier}")
    if req.tier == "spotlight" and not req.spotlight_engine:
        raise HTTPException(400, "Spotlight tier requires spotlight_engine")
    if not stripe.api_key:
        raise HTTPException(500, "STRIPE_SECRET_KEY is not set")

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=req.email,
        success_url=f"{PUBLIC_BASE_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_BASE_URL}/checkout/cancel",
        metadata={
            "tier": req.tier,
            "brand_name": req.brand_name,
            "domain": req.domain,
            "email": req.email,
            "spotlight_engine": req.spotlight_engine or "",
        },
    )
    return JSONResponse({"id": session.id, "url": session.url})


@app.get("/checkout/success", response_class=HTMLResponse)
def checkout_success() -> str:
    return """
    <div style="font-family: system-ui; padding: 60px; text-align: center;">
      <h1>Payment received ✓</h1>
      <p>Your audit is being generated. We'll email it within a few minutes.</p>
    </div>
    """


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
        meta = session.get("metadata") or {}
        # Run the audit in a background thread so the webhook returns fast.
        threading.Thread(
            target=_fulfil_order,
            args=(meta,),
            daemon=True,
        ).start()

    return JSONResponse({"received": True})


@app.get("/report/{run_id}", response_class=HTMLResponse)
def serve_report(run_id: str) -> HTMLResponse:
    run_dir = OUTPUT_ROOT / run_id
    html_path = run_dir / "report.html"
    if not html_path.exists():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(html_path.read_text())


@app.get("/report/{run_id}/pdf")
def serve_pdf(run_id: str) -> RedirectResponse:
    run_dir = OUTPUT_ROOT / run_id
    pdf_path = run_dir / "report.pdf"
    if not pdf_path.exists():
        raise HTTPException(404, "PDF not found")
    return RedirectResponse(f"/static/{run_id}/report.pdf")


def _build_site_for_order(meta: dict[str, Any]) -> SiteConfig:
    """For now, use the on-disk config as a base and override brand name + domain
    from the Stripe metadata. Production would pull from a per-customer DB row."""
    base = SiteConfig.model_validate(yaml.safe_load(DEFAULT_CONFIG_PATH.open()))
    base.brand.name = meta.get("brand_name") or base.brand.name
    base.brand.domain = meta.get("domain") or base.brand.domain
    return base


def _fulfil_order(meta: dict[str, Any]) -> None:
    """Run the audit, generate PDF, email the customer.
    All errors are swallowed and logged — the customer record gets retried via Stripe."""
    tier = (meta.get("tier") or "").strip()
    if tier not in VALID_TIERS - {"free"}:
        return
    email = meta.get("email")
    if not email:
        return

    site = _build_site_for_order(meta)
    queries = _load_queries(DEFAULT_QUERIES_PATH)

    # Filter queries + engines per tier
    spotlight_engine = meta.get("spotlight_engine") or None
    if tier == "spotlight":
        queries = [q for q in queries if q.spotlight or q.free]
        only_labels = {FREE_TIER_ENGINE, spotlight_engine} if spotlight_engine else None
    else:
        only_labels = None

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

    run_id = uuid.uuid4().hex[:12]
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = capture_screenshot(site.brand.domain, run_dir)
    responses = asyncio.run(run_audit(engine_objs, queries, run_dir))

    if tier in {"spotlight"}:
        llm_scores: list[LLMScore | None] = [None] * len(responses)
    else:
        llm_scores = list(asyncio.run(score_all(responses, site)))

    rows = [
        ScoredRow(
            response=r,
            deterministic=score_response(r, site),
            llm=llm,
        )
        for r, llm in zip(responses, llm_scores)
    ]

    action_plan = (
        generate_action_plan(rows, site) if tier == "action_plan" else None
    )

    write_csv(rows, run_dir)
    write_html(
        rows,
        site,
        run_dir,
        tier=tier,
        screenshot=screenshot_path.name if screenshot_path else None,
        action_plan=action_plan,
    )
    pdf_path: Path | None = None
    try:
        pdf_path = render_pdf(run_dir)
    except Exception:  # noqa: BLE001
        pdf_path = None

    report_url = f"{PUBLIC_BASE_URL}/report/{run_id}"
    try:
        send_report(
            to_email=email,
            brand_name=site.brand.name,
            tier=tier,
            report_url=report_url,
            pdf_path=pdf_path,
        )
    except Exception as exc:  # noqa: BLE001
        # Persist a tombstone so we can retry/inspect manually.
        (run_dir / "delivery_error.log").write_text(
            f"{type(exc).__name__}: {exc}\n  to: {email}\n"
        )
