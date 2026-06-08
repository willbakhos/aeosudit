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
import re
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
DEFAULT_ENGINES = ["Google AI Mode"]
CHATGPT_LABEL = "ChatGPT"

# Per-tier run quota (scheduled cron + manual reruns, combined).
# Paid subscribers: 1 scheduled + 1 manual = 2/month.
# Free dashboard brands: no scheduled run, 1 manual/month.
MONTHLY_RUN_QUOTA = 2  # legacy alias for the paid quota; prefer _monthly_run_quota()


def _monthly_run_quota(tier: str | None) -> int:
    """Total run cap for a tracked brand per calendar month, including the
    scheduled monthly cron + any manual reruns the user triggers from the
    dashboard. Free brands get 1 manual; paid get 1 scheduled + 1 manual."""
    return 2 if (tier or "").strip() else 1


def _engines_for_brand(brand: TrackedBrand) -> set[str] | None:
    """Resolve which engines should run for a brand based on its tier.
    Returns None to mean "no filter / all configured engines".

    two_engine_monthly -> {Google AI Mode, ChatGPT}
    full_monthly       -> all engines
    no tier            -> Google AI Mode only (free dashboard view)
    """
    if brand.tier == "full_monthly":
        return None  # no filter — all configured engines
    if brand.tier == "two_engine_monthly":
        return {"Google AI Mode", CHATGPT_LABEL}
    # Free dashboard usage: just Google AI Mode
    return {"Google AI Mode"}


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
def login_page(sent: int = 0, error: str = "", claim: str = "", email: str = "") -> HTMLResponse:
    body = _render(
        "login.html.j2",
        sent=bool(sent),
        error=error or None,
        configured=supabase_configured(),
        claim=(claim or None),
        prefilled_email=(email or ""),
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
def login_submit(
    request: Request,
    email: str = Form(...),
    claim: str = Form(""),
) -> RedirectResponse:
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
    resp = RedirectResponse("/dashboard/login?sent=1", status_code=303)
    # When the form came from the in-report "Save this report" widget it
    # includes a claim=<run_id>. Stash it as a cookie so the post-magic-link
    # /dashboard handler can hydrate the brand + run for the user.
    if claim:
        resp.set_cookie(
            CLAIM_COOKIE, claim.strip(),
            max_age=3600, samesite="lax",
            secure=os.environ.get("COOKIE_SECURE", "1") == "1",
            httponly=False, path="/",
        )
    return resp


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
    """Dashboard landing page. Handles the post-signup 'claim a free preview'
    cookie flow, then drops the user on /reports — the most common 'what
    happened with my brands?' view. Brands list is one tab away."""
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
            f"/dashboard/brands/{brand_id}?claimed=1" if brand_id else "/dashboard/reports"
        )
        resp = RedirectResponse(target, status_code=303)
        resp.delete_cookie(CLAIM_COOKIE, path="/")
        return resp
    # No claim cookie — send the user to Reports (the friendliest 'what's
    # happening?' view). Brands list is one tab over at /dashboard/brands.
    return RedirectResponse("/dashboard/reports", status_code=303)


@router.get("/brands", response_class=HTMLResponse)
def brands_list(request: Request):
    """All brands the user is tracking, with their latest run summary."""
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
    """Legacy textarea parser — kept for any caller still passing the old
    'Name | domain.com' string format. Modern routes use the structured
    form via _competitors_from_form."""
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


def _competitors_from_form(form) -> list[dict[str, str]]:
    """Read paired competitor_name + competitor_domain fields from a form
    submission and return the list of {name, domain} dicts. Drops rows
    where both fields are empty so users can leave the seed row blank."""
    names = form.getlist("competitor_name")
    domains = form.getlist("competitor_domain")
    out: list[dict[str, str]] = []
    for n, d in zip(names, domains):
        n_clean = (n or "").strip()
        d_clean = (d or "").strip()
        if n_clean or d_clean:
            out.append({"name": n_clean, "domain": d_clean})
    return out


@router.post("/brands")
async def brand_create(
    request: Request,
    name: str = Form(...),
    domain: str = Form(...),
    aliases: str = Form(""),
    ground_truth: str = Form(""),
    locale_country: str = Form("US"),
    locale_language: str = Form("en"),
    tier: str = Form(""),
):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    competitor_rows = _competitors_from_form(form)
    # Tier upgrades go through Stripe — only master accounts can self-assign
    # a paid tier from this form. Regular users always start at free.
    valid_tiers = {"", "two_engine_monthly", "full_monthly"}
    requested_tier = tier.strip() if tier.strip() in valid_tiers else ""
    tier_clean = requested_tier if user.get("is_master") else ""
    brand = TrackedBrand(
        user_id=UUID(user["id"]),
        name=name.strip(),
        domain=domain.strip(),
        aliases=[a.strip() for a in aliases.split(",") if a.strip()],
        competitors=competitor_rows,
        ground_truth=[g.strip() for g in ground_truth.splitlines() if g.strip()],
        engines=list(DEFAULT_ENGINES),
        locale_country=(locale_country.strip() or "US").upper(),
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
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
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

    quota = _monthly_run_quota(brand.tier)
    used = brand.runs_this_month or 0
    return _render(
        "brand_detail.html.j2",
        user=user,
        brand=brand,
        runs=list(reversed(runs)),  # newest first in the table
        trend_json=json.dumps(trend),
        latest=latest_complete,
        monthly_quota=quota,
        runs_used=used,
        runs_left=max(0, quota - used),
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
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
            raise HTTPException(404, "Brand not found")
        s.expunge(brand)
    return _render("brand_form.html.j2", user=user, brand=brand, active_tab="brands")


@router.post("/brands/{brand_id}/edit")
async def brand_update(
    request: Request,
    brand_id: str,
    name: str = Form(...),
    domain: str = Form(...),
    aliases: str = Form(""),
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
    form = await request.form()
    competitor_rows = _competitors_from_form(form)
    valid_tiers = {"", "two_engine_monthly", "full_monthly"}
    requested_tier = tier.strip() if tier.strip() in valid_tiers else ""
    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
            raise HTTPException(404, "Brand not found")
        was_subscriber = bool(brand.tier)
        # Tier changes only honoured for master accounts; everyone else
        # keeps whatever tier they have (Stripe is the upgrade path).
        if user.get("is_master"):
            tier_clean = requested_tier
        else:
            tier_clean = brand.tier or ""
        brand.name = name.strip()
        brand.domain = domain.strip()
        brand.aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        brand.competitors = competitor_rows
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
    "google_only": {"Google AI Mode"},
    "two_engine": {"Google AI Mode", CHATGPT_LABEL},
    "full": None,  # None means "no filter / all configured engines"
}

# Add-on run pricing per subscription tier. USD, charged off-session to the
# customer's saved card via PaymentIntent. None means "tier can't buy extras".
EXTRA_RUN_PRICE_USD: dict[str, int] = {
    "two_engine_monthly": 10,
    "full_monthly": 25,
}


def _backfill_stripe_customer(brand: TrackedBrand, email: str, session) -> str | None:
    """Look up the brand owner's Stripe customer by email and cache the ID
    on the brand row. Returns None if Stripe isn't configured, the email
    has no matching customer, or the lookup fails."""
    import stripe
    if not stripe.api_key or not email:
        return None
    try:
        result = stripe.Customer.list(email=email, limit=1)
        if result.data:
            cid = result.data[0].id
            brand.stripe_customer_id = cid
            session.add(brand)
            session.commit()
            return cid
    except Exception as exc:  # noqa: BLE001
        print(f"[buy-extra] customer lookup failed: {type(exc).__name__}: {exc}")
    return None


@router.post("/brands/{brand_id}/runs")
def brand_run(
    request: Request,
    brand_id: str,
    engine_set: str = Form(""),
    force: str = Form(""),
):
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
    # Only masters can bypass the monthly quota — and only by explicitly
    # ticking the override box (so it's a deliberate action, not silent).
    force_override = bool(force) and user.get("is_master")
    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
            raise HTTPException(404, "Brand not found")
        _reset_monthly_counter_if_needed(brand)
        quota = _monthly_run_quota(brand.tier)
        used = brand.runs_this_month or 0
        if used >= quota and not force_override:
            # Friendly redirect with a flash-style query param the UI can render.
            return RedirectResponse(
                f"/dashboard/monitoring?quota_reached={brand_id}",
                status_code=303,
            )
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
    if user.get("is_master"):
        thread_kwargs["force_full_report"] = True
    threading.Thread(
        target=_run_audit_for_brand,
        args=thread_args,
        kwargs=thread_kwargs,
        daemon=True,
    ).start()
    return RedirectResponse(f"/dashboard/brands/{brand_id}", status_code=303)


@router.post("/brands/{brand_id}/runs/buy-extra")
def buy_extra_run(request: Request, brand_id: str):
    """Charge the brand owner's saved card for an extra audit run, then kick
    the audit off immediately. Off-session PaymentIntent — no Stripe redirect
    on the happy path. If the card needs 3DS authentication, falls back to a
    Stripe Checkout Session URL the client opens in a new tab."""
    import stripe
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        raise HTTPException(401, "Sign in required")
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    if not stripe.api_key:
        return JSONResponse({"error": "Payment processing is not configured."}, status_code=503)

    from src.server import _make_run_id, PUBLIC_BASE_URL

    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
            raise HTTPException(404, "Brand not found")
        price_usd = EXTRA_RUN_PRICE_USD.get(brand.tier or "")
        if not price_usd:
            return JSONResponse(
                {"error": "Extra runs are only available on monitoring subscriptions."},
                status_code=400,
            )

        # Master bypass — skip Stripe entirely so internal testing of the
        # buy-extra flow doesn't actually charge a card. The same modal +
        # success path still fires; the client surfaces a 'no card charged'
        # note. force_full_report=True so the test report has the action
        # plan + tech audit a paying subscriber would see.
        if user.get("is_master"):
            run_rec = AuditRunRecord(
                brand_id=brand.id,
                user_id=brand.user_id,
                run_id=_make_run_id(brand.name),
                status="running",
            )
            s.add(run_rec)
            s.commit()
            s.refresh(run_rec)
            run_record_id_master = str(run_rec.id)
            threading.Thread(
                target=_run_audit_for_brand,
                args=(str(bid), run_record_id_master),
                kwargs={"force_full_report": True},
                daemon=True,
            ).start()
            return JSONResponse({
                "ok": True,
                "master_test": True,
                "brand_id": str(bid),
                "run_record_id": run_record_id_master,
            })

        # Resolve the Stripe customer (cached on brand, or look up by email).
        customer_id = brand.stripe_customer_id or _backfill_stripe_customer(brand, user["email"], s)
        if not customer_id:
            return JSONResponse(
                {"error": "We couldn't find your payment method on file. Update your card in the billing portal and try again."},
                status_code=400,
            )
        # Find the customer's default payment method (or first attached card).
        try:
            customer = stripe.Customer.retrieve(customer_id)
            inv = customer.get("invoice_settings") if isinstance(customer, dict) else customer.invoice_settings
            pm_id = (inv or {}).get("default_payment_method") if isinstance(inv, dict) else getattr(inv, "default_payment_method", None)
            if not pm_id:
                pms = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
                if pms.data:
                    pm_id = pms.data[0].id
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"Could not access saved card: {exc}"}, status_code=400)
        if not pm_id:
            return JSONResponse(
                {"error": "No saved card found on your account. Add one in the billing portal."},
                status_code=400,
            )

        # Attempt the off-session charge.
        amount_cents = int(price_usd) * 100
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency="usd",
                customer=customer_id,
                payment_method=pm_id,
                off_session=True,
                confirm=True,
                description=f"Extra monitoring run — {brand.name}",
                metadata={
                    "brand_id": str(brand.id),
                    "tier": brand.tier or "",
                    "type": "extra_run",
                },
            )
            paid_ok = (intent.status == "succeeded")
        except stripe.error.CardError as exc:
            # 3DS / authentication_required path — fall back to a hosted
            # Checkout Session so the user can authenticate in a new tab.
            code = getattr(exc.error, "code", "") or ""
            if code == "authentication_required":
                try:
                    sess = stripe.checkout.Session.create(
                        mode="payment",
                        customer=customer_id,
                        line_items=[{
                            "price_data": {
                                "currency": "usd",
                                "unit_amount": amount_cents,
                                "product_data": {"name": f"Extra audit run — {brand.name}"},
                            },
                            "quantity": 1,
                        }],
                        metadata={"brand_id": str(brand.id), "type": "extra_run"},
                        success_url=f"{PUBLIC_BASE_URL}/dashboard/brands/{brand.id}/runs/buy-extra/confirm?session_id={{CHECKOUT_SESSION_ID}}",
                        cancel_url=f"{PUBLIC_BASE_URL}/dashboard/monitoring",
                    )
                    return JSONResponse(
                        {"needs_auth": True, "checkout_url": sess.url},
                        status_code=402,
                    )
                except Exception as inner:  # noqa: BLE001
                    return JSONResponse({"error": f"Card needs verification ({inner})"}, status_code=402)
            return JSONResponse(
                {"error": getattr(exc.error, "message", str(exc))},
                status_code=402,
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=500)

        if not paid_ok:
            return JSONResponse(
                {"error": f"Payment did not complete (status: {intent.status})."},
                status_code=402,
            )

        # Charge succeeded — create the run record and spawn the audit.
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
    return JSONResponse({"ok": True, "brand_id": str(bid), "run_record_id": run_record_id})


@router.get("/brands/{brand_id}/runs/buy-extra/confirm")
def buy_extra_run_confirm(request: Request, brand_id: str, session_id: str = ""):
    """Stripe Checkout Session success URL for the 3DS fallback. Verifies the
    session was paid, then spawns the audit run and bounces the user back to
    the brand detail page."""
    import stripe
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        bid = UUID(brand_id)
    except ValueError:
        raise HTTPException(404, "Brand not found")
    if not session_id or not stripe.api_key:
        return RedirectResponse(f"/dashboard/monitoring?extra_failed=1", status_code=303)
    try:
        sess = stripe.checkout.Session.retrieve(session_id)
    except Exception:  # noqa: BLE001
        return RedirectResponse(f"/dashboard/monitoring?extra_failed=1", status_code=303)
    paid = (getattr(sess, "payment_status", "") == "paid")
    if not paid:
        return RedirectResponse(f"/dashboard/monitoring?extra_failed=1", status_code=303)

    from src.server import _make_run_id
    with get_session() as s:
        brand = s.get(TrackedBrand, bid)
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
            raise HTTPException(404, "Brand not found")
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
    return RedirectResponse(f"/dashboard/brands/{brand_id}?extra_paid=1", status_code=303)


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
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
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
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
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
        if not brand or (str(brand.user_id) != user["id"] and not user.get("is_master")):
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
# Sidebar order mirrors the actual section order in templates/report.html.j2
# so users see the same flow in the nav as they do when they scroll.
# If you reorder sections in report.html.j2, mirror the change here.
REPORT_SECTIONS: list[dict[str, str]] = [
    {"label": "Headline metrics", "anchor": "headline-metrics", "fallback": "visibility"},
    {"label": "Technical foundations", "anchor": "technical-foundations", "fallback": "foundations"},
    {"label": "Action plan", "anchor": "action-plan", "fallback": "unlock"},
    {"label": "Engine heatmap", "anchor": "engine-heatmap", "fallback": "engines"},
    {"label": "Top cited sources", "anchor": "top-cited-sources", "fallback": "sources"},
    {"label": "Competitor share-of-voice", "anchor": "competitor-sov", "fallback": "sources"},
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

    # The DB record exists, but on Railway's ephemeral filesystem the report
    # artifacts get wiped on every deploy. Detect that here so the user sees
    # a styled re-run CTA instead of a raw JSON 404 inside the iframe.
    from src.server import OUTPUT_ROOT
    run_dir = OUTPUT_ROOT / run_id
    has_html = (run_dir / "report.html").exists()
    has_raw = (run_dir / "raw_responses.json").exists()
    if not has_html and not has_raw:
        return _render(
            "report_unavailable.html.j2",
            user=user,
            run=run,
            brand=brand,
            active_tab="reports",
        )

    return _render(
        "report_view.html.j2",
        user=user,
        run=run,
        brand=brand,
        sections=REPORT_SECTIONS,
        active_tab="reports",
    )


@router.get("/reports/{run_id}/csv")
def report_csv_download(request: Request, run_id: str):
    """Stream the raw results.csv produced when the audit ran. Auth-gated
    the same way as /reports/{run_id}: the run must belong to the logged-in
    user, or the viewer must be a master account. The CSV file is the same
    one write_csv(rows, run_dir) drops next to the HTML report."""
    from fastapi.responses import FileResponse
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    with get_session() as s:
        run = s.exec(
            select(AuditRunRecord).where(AuditRunRecord.run_id == run_id)
        ).first()
        if not run:
            raise HTTPException(404, "Report not found")
        if str(run.user_id) != user["id"] and not user.get("is_master"):
            raise HTTPException(404, "Report not found")
        brand = s.get(TrackedBrand, run.brand_id)
    output_root = Path(os.environ.get("OUTPUT_ROOT", "output"))
    csv_path = output_root / run_id / "results.csv"
    if not csv_path.exists():
        # Older runs predate write_csv or the file was pruned. Be honest
        # rather than 500ing — the report still works, just no CSV.
        raise HTTPException(404, "CSV not available for this run")
    # Friendly filename so the browser saves it as "<brand>-<run_id>.csv"
    # instead of the bare "results.csv".
    safe_brand = "".join(c if c.isalnum() else "-" for c in (brand.name if brand else "report")).strip("-").lower() or "report"
    filename = f"monitoraeo-{safe_brand}-{run_id}.csv"
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


@router.get("/api/pending-audit", response_class=JSONResponse)
def pending_audit_status(request: Request) -> JSONResponse:
    """Tells the empty-dashboard banner whether the logged-in user has a
    paid audit currently mid-flight, and if so what stage it's at. Used
    by JS polling so the user sees real progress instead of guessing.

    Consumes 'complete' jobs on the first poll that reports them: the JS
    schedules window.location.reload() 1.2s after seeing status=complete,
    and without consumption the next poll after reload would *also* see
    complete and trigger another reload — an infinite refresh loop that
    presented as the banner flashing forever on 'Done — opening your
    dashboard'. After we return complete once, subsequent polls return
    idle and the banner stays hidden."""
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"status": "unauthenticated"}, status_code=401)
    from src.server import PAID_AUDIT_JOBS
    key = (user.get("email") or "").strip().lower()
    job = PAID_AUDIT_JOBS.get(key) if key else None
    if not job:
        return JSONResponse({"status": "idle"})
    if job.get("status") == "complete" and key:
        PAID_AUDIT_JOBS.pop(key, None)
    return JSONResponse(job)


@router.post("/api/recover-paid-orders", response_class=JSONResponse)
def recover_paid_orders(request: Request) -> JSONResponse:
    """Self-serve recovery: scan output/_paid_orders/ for paid order metas
    matching the logged-in user's email and run hydration for any that
    didn't make it into the dashboard the first time. Idempotent — re-runs
    just see the brand already exists and no-op."""
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"status": "unauthenticated"}, status_code=401)
    email = (user.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"recovered": 0, "checked": 0})

    output_root = Path(os.environ.get("OUTPUT_ROOT", "output"))
    pending_dir = output_root / "_paid_orders"
    if not pending_dir.exists():
        return JSONResponse({"recovered": 0, "checked": 0})

    from src.server import (
        _build_site_for_order,
        _ensure_dashboard_for_paid_order,
        _generate_paid_queries,
        _make_run_id,
        TIER_PLANS,
    )

    # PAID_AUDIT_JOBS holds in-flight fulfilment jobs keyed by buyer email.
    # If a job is currently running for this user, we MUST NOT create a stub
    # for the same brand — the real fulfilment will write the proper run
    # record (with rows + action plan) when it finishes. Without this check,
    # users who click the magic link mid-audit (the common case, since the
    # email lands ~30s in and the audit takes 5-10 min) saw a stub row with
    # queries=1 appear alongside the in-flight banner.
    from src.server import PAID_AUDIT_JOBS as _PAID_AUDIT_JOBS

    checked = 0
    recovered = 0
    for meta_path in pending_dir.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:  # noqa: BLE001
            continue
        meta_email = (meta.get("email") or "").strip().lower()
        if meta_email != email:
            continue
        checked += 1
        tier = (meta.get("tier") or "").strip()
        if tier not in TIER_PLANS:
            continue

        # Skip 1: in-flight fulfilment for this same brand. The active
        # _fulfil_order_inner thread will create the run record itself
        # when its engine + LLM + action-plan pipeline finishes. Leave
        # the meta file alone — that thread also owns archiving it.
        active = _PAID_AUDIT_JOBS.get(email) or {}
        active_status = active.get("status")
        active_brand = (active.get("brand") or "").strip().lower()
        meta_brand = (meta.get("brand_name") or "").strip().lower()
        if active_status == "running" and active_brand and active_brand == meta_brand:
            continue

        # Skip 2: a real run already landed for this brand+user (e.g. the
        # fulfilment thread finished but its archive step didn't run, or
        # the user is reloading after a previous successful purchase).
        # Archive the orphan meta to stop it being seen on the next reload.
        try:
            domain_norm = (meta.get("domain") or "").strip().lower()
            recently = None
            if domain_norm:
                with get_session() as s:
                    brand_match = s.exec(
                        select(TrackedBrand).where(
                            TrackedBrand.user_id == UUID(user["id"]),
                            TrackedBrand.domain == domain_norm,
                        )
                    ).first()
                    if brand_match:
                        recently = s.exec(
                            select(AuditRunRecord)
                            .where(
                                AuditRunRecord.brand_id == brand_match.id,
                                AuditRunRecord.status == "complete",
                                AuditRunRecord.queries_total > 1,
                            )
                            .order_by(AuditRunRecord.started_at.desc())
                        ).first()
            if recently is not None:
                # Already fulfilled — archive the orphan meta and skip.
                archived = pending_dir / "_recovered"
                archived.mkdir(exist_ok=True)
                meta_path.rename(archived / meta_path.name)
                continue
        except Exception as exc:  # noqa: BLE001
            print(f"[recover-paid-orders] duplicate-check failed for {meta_path.name}: {type(exc).__name__}: {exc}")
        try:
            site = _build_site_for_order(meta)
            # Use a stable run_id for recovery so we don't create duplicate
            # records on repeat clicks. Caller is idempotent on (user, domain).
            run_id = meta.get("run_id") or _make_run_id(site.brand.name)
            # Seed monitored_queries from the same 40 paid queries the
            # original audit would have used. Without this, subsequent
            # dashboard re-runs fall through to the 8-query generic set
            # and the customer gets a thin report with no real action plan.
            paid_queries = _generate_paid_queries(
                site.brand.name, list(site.competitors or [])
            )
            _ensure_dashboard_for_paid_order(
                email=email, tier=tier, site=site, run_id=run_id, rows=[],
                monitored_queries=[q.query for q in paid_queries],
            )
            recovered += 1
            # Successful hydration → archive the meta so we don't process it again.
            archived = pending_dir / "_recovered"
            archived.mkdir(exist_ok=True)
            meta_path.rename(archived / meta_path.name)
        except Exception as exc:  # noqa: BLE001
            print(f"[recover-paid-orders] failed for {meta_path.name}: {type(exc).__name__}: {exc}")

    return JSONResponse({"recovered": recovered, "checked": checked})


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
# Support tab — let logged-in users send us a ticket without leaving the app.
# Email auto-prefills from their account, brand context is injected into the
# email body so support replies have what they need without back-and-forth.
# ---------------------------------------------------------------------------
@router.get("/support", response_class=HTMLResponse)
def support_form(request: Request, status: str = ""):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return _render(
        "support.html.j2",
        user=user,
        status=status or None,
        active_tab="support",
    )


@router.post("/support", response_class=HTMLResponse)
def submit_support_ticket(
    request: Request,
    subject: str = Form(...),
    topic: str = Form("general"),
    message: str = Form(...),
):
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    # Build the user-context block so support sees who's writing without
    # us having to grep the DB. Include account id, tier, brands tracked.
    brand_lines: list[str] = []
    try:
        with get_session() as s:
            brands = list(
                s.exec(
                    select(TrackedBrand).where(
                        TrackedBrand.user_id == UUID(user["id"])
                    )
                )
            )
            for b in brands[:10]:
                brand_lines.append(
                    f"<li><strong>{b.name}</strong> ({b.domain}) — tier "
                    f"{(b.tier or 'one-off')!r}, {b.runs_this_month or 0} runs this month</li>"
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[support] brand-context lookup failed: {type(exc).__name__}: {exc}")
    context = (
        f"<p><strong>Account:</strong> {user.get('email')} "
        f"(<code>{user.get('id')}</code>){' — MASTER' if user.get('is_master') else ''}</p>"
        + (
            f"<p><strong>Brands tracked:</strong></p><ul>{''.join(brand_lines)}</ul>"
            if brand_lines
            else "<p><em>No brands tracked yet.</em></p>"
        )
    )
    from src.server import send_support_ticket
    sent = send_support_ticket(
        email=user.get("email") or "",
        subject=subject,
        topic=topic,
        message=message,
        context=context,
    )
    return RedirectResponse(
        f"/dashboard/support?status={'sent' if sent else 'error'}",
        status_code=303,
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
                "triggers": [(r.trigger_type or "manual") for r in runs],
                "visibility": _series("visibility_rate"),
                "citation": _series("citation_rate"),
                "sentiment": _series("sentiment_avg"),
                "accuracy": _series("accuracy_avg"),
                "hallucinations": _series("hallucination_rate"),
                "sov_labels": [c for c, _ in latest_sov],
                "sov_counts": [n for _, n in latest_sov],
            }
            quota = _monthly_run_quota(b.tier)
            used = b.runs_this_month or 0
            rows.append({
                "brand": b,
                "trend_runs": runs,
                "trend_json": json.dumps(chart_payload),
                "next_scheduled": b.next_scheduled_run or _compute_next_scheduled_run(),
                "monthly_quota": quota,
                "runs_used": used,
                "runs_left": max(0, quota - used),
            })
    return _render("monitoring.html.j2", user=user, rows=rows, active_tab="monitoring")


# ---------------------------------------------------------------------------
# Admin — master-only super-admin surface
# ---------------------------------------------------------------------------

def _require_master(request: Request):
    """Same as _require_user but additionally enforces is_master.
    Returns 404 (not 403) on non-master so the URL doesn't even hint at
    the existence of an admin surface for regular users."""
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if not user.get("is_master"):
        raise HTTPException(404, "Not found")
    return user


def _list_auth_users() -> list[dict]:
    """Read every Supabase auth.users row via the existing Postgres
    connection. Returns [] gracefully if the auth schema isn't reachable
    (e.g. local dev with no DATABASE_URL). Sorted newest-first."""
    from sqlalchemy import text
    from src.db import engine
    try:
        eng = engine()
    except RuntimeError:
        return []
    rows: list[dict] = []
    try:
        with eng.connect() as conn:
            result = conn.execute(text(
                "SELECT id, email, created_at, last_sign_in_at, "
                "       email_confirmed_at "
                "FROM auth.users ORDER BY created_at DESC"
            ))
            for r in result.mappings():
                rows.append({
                    "id": str(r["id"]),
                    "email": r["email"] or "",
                    "created_at": r["created_at"],
                    "last_sign_in_at": r["last_sign_in_at"],
                    "email_confirmed_at": r["email_confirmed_at"],
                })
    except Exception as exc:  # noqa: BLE001
        print(f"[admin] auth.users query failed: {type(exc).__name__}: {exc}")
        return []
    return rows


_TIER_SHORT_LABELS = {
    "two_engine": "2-engine audit",
    "full_audit": "5-engine audit",
    "two_engine_monthly": "2-engine monitoring",
    "full_monthly": "5-engine monitoring",
}


def _short_tier(tier: str) -> str:
    return _TIER_SHORT_LABELS.get(tier, tier or "—")


def _resolve_admin_range(
    range_key: str, start_str: str, end_str: str,
) -> tuple[datetime | None, datetime | None, str]:
    """Translate a UI range selection into (start_dt, end_dt, normalised_key).

    Returns (None, None, 'all') for the 'all time' default. Otherwise both
    bounds are UTC datetimes; the upper bound is exclusive (e.g. 'today'
    spans [today 00:00, tomorrow 00:00)). Falls back to 'all' on parse
    errors in custom inputs so a bad URL doesn't 500 the admin page."""
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    key = (range_key or "all").lower()
    if key == "today":
        return today_start, today_start + timedelta(days=1), "today"
    if key == "yesterday":
        return today_start - timedelta(days=1), today_start, "yesterday"
    if key == "7d":
        return today_start - timedelta(days=6), today_start + timedelta(days=1), "7d"
    if key == "30d":
        return today_start - timedelta(days=29), today_start + timedelta(days=1), "30d"
    if key == "month":
        m_start = datetime(now.year, now.month, 1)
        next_m = datetime(now.year + 1, 1, 1) if now.month == 12 else datetime(now.year, now.month + 1, 1)
        return m_start, next_m, "month"
    if key == "last_month":
        m_start = datetime(now.year, now.month, 1)
        prev_m_end = m_start
        prev_m_start = datetime(now.year - 1, 12, 1) if now.month == 1 else datetime(now.year, now.month - 1, 1)
        return prev_m_start, prev_m_end, "last_month"
    if key == "year":
        return datetime(now.year, 1, 1), datetime(now.year + 1, 1, 1), "year"
    if key == "custom":
        try:
            sd = datetime.strptime(start_str, "%Y-%m-%d") if start_str else None
            ed = datetime.strptime(end_str, "%Y-%m-%d") + timedelta(days=1) if end_str else None
            # If both bounds are missing, fall back to 'all'.
            if sd is None and ed is None:
                return None, None, "all"
            return sd, ed, "custom"
        except ValueError:
            return None, None, "all"
    return None, None, "all"


@router.get("/admin", response_class=HTMLResponse)
def admin_index(
    request: Request,
    deleted: int = 0,
    error: str = "",
    range: str = "all",
    start: str = "",
    end: str = "",
    q: str = "",
    page: int = 1,
    per: int = 50,
):
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user
    auth_users = _list_auth_users()

    start_dt, end_dt, range_normalised = _resolve_admin_range(range, start, end)
    has_range = start_dt is not None or end_dt is not None

    # One-shot fetch of every brand + (filtered) run + (filtered) purchase,
    # then group in Python. Brands themselves are not date-filtered — the
    # date range only scopes activity (runs / purchases / revenue) so the
    # admin can ask 'who paid us in March' vs 'who's signed up overall'.
    from src.db import Purchase
    with get_session() as s:
        all_brands = list(s.exec(select(TrackedBrand)))
        run_q = select(AuditRunRecord)
        purchase_q = select(Purchase)
        if start_dt is not None:
            run_q = run_q.where(AuditRunRecord.started_at >= start_dt)
            purchase_q = purchase_q.where(Purchase.created_at >= start_dt)
        if end_dt is not None:
            run_q = run_q.where(AuditRunRecord.started_at < end_dt)
            purchase_q = purchase_q.where(Purchase.created_at < end_dt)
        all_runs = list(s.exec(run_q))
        all_purchases = list(s.exec(purchase_q))
        # Latest run for each user is "all-time latest" regardless of range,
        # because the 'last seen' signal is useful independent of the filter.
        latest_runs_all = list(s.exec(select(AuditRunRecord)))

    brands_by_user: dict[str, list[TrackedBrand]] = {}
    for b in all_brands:
        brands_by_user.setdefault(str(b.user_id), []).append(b)
    runs_by_user: dict[str, list[AuditRunRecord]] = {}
    for r in all_runs:
        runs_by_user.setdefault(str(r.user_id), []).append(r)
    latest_by_user: dict[str, datetime] = {}
    for r in latest_runs_all:
        key = str(r.user_id)
        if key not in latest_by_user or r.started_at > latest_by_user[key]:
            latest_by_user[key] = r.started_at
    # Purchases are matched to users by lowercase email (Stripe doesn't
    # know about Supabase user_ids).
    purchases_by_email: dict[str, list[Purchase]] = {}
    for p in all_purchases:
        purchases_by_email.setdefault((p.email or "").lower(), []).append(p)

    from src.server import TIER_PLANS
    summaries = []
    for u in auth_users:
        brands = brands_by_user.get(u["id"], [])
        runs = runs_by_user.get(u["id"], [])
        purchases = purchases_by_email.get((u["email"] or "").lower(), [])
        active_subs = [
            {"brand": b.name, "tier": b.tier, "label": TIER_PLANS.get(b.tier, {}).get("label", b.tier)}
            for b in brands if b.tier
        ]
        summaries.append({
            **u,
            "brand_count": len(brands),
            "run_count": len(runs),
            "active_subs": active_subs,
            "is_master": is_master_email(u["email"]),
            "latest_run_at": latest_by_user.get(u["id"]),
            "purchase_count": len(purchases),
            "revenue_usd": round(sum(p.amount_usd or 0 for p in purchases), 2),
        })

    # Search filter — case-insensitive substring match on email.
    if q.strip():
        ql = q.strip().lower()
        summaries = [s for s in summaries if ql in (s.get("email") or "").lower()]

    # When a date range is active, hide users with no activity in the window
    # so the table shows ONLY who was active. With 'all time' we still want
    # the full user list (including dormant accounts) for moderation.
    if has_range:
        summaries = [s for s in summaries if s["run_count"] or s["purchase_count"]]

    # Sort: revenue desc, then run count desc, then signup recency desc.
    summaries.sort(
        key=lambda x: (x.get("revenue_usd") or 0, x.get("run_count") or 0, x.get("created_at") or datetime.min),
        reverse=True,
    )

    # Pagination — clamp per to 10..200, page to >=1.
    per = max(10, min(per or 50, 200))
    page = max(1, page or 1)
    total = len(summaries)
    total_pages = max(1, (total + per - 1) // per)
    page = min(page, total_pages)
    start_idx = (page - 1) * per
    end_idx = start_idx + per
    page_summaries = summaries[start_idx:end_idx]

    # Range total = sum of revenue across the filtered (search + date) set.
    grand_total = round(sum(s["revenue_usd"] for s in summaries), 2)

    return _render(
        "admin_users.html.j2",
        user=user,
        users=page_summaries,
        grand_revenue_usd=grand_total,
        flash_deleted=bool(deleted),
        flash_error=error or None,
        active_tab="admin",
        # Filter / pagination state for the template.
        range_value=range_normalised,
        start_value=start,
        end_value=end,
        search_value=q,
        page=page,
        per=per,
        total=total,
        total_pages=total_pages,
        start_idx=start_idx + 1 if total else 0,
        end_idx=min(end_idx, total),
        has_range=has_range,
    )


def is_master_email(email: str) -> bool:
    """Module-level alias so templates can call this via dashboard import."""
    from src.auth import is_master
    return is_master(email)


@router.get("/admin/export.csv")
def admin_export_csv(
    request: Request,
    range: str = "all",
    start: str = "",
    end: str = "",
    q: str = "",
):
    """Download the currently-filtered user list as CSV. Reuses the same
    range/search semantics as GET /admin so 'Download CSV' from the admin
    UI always returns exactly what's on screen. No pagination — the CSV
    contains every matching user."""
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user
    auth_users = _list_auth_users()
    start_dt, end_dt, _ = _resolve_admin_range(range, start, end)
    has_range = start_dt is not None or end_dt is not None

    from src.db import Purchase
    with get_session() as s:
        all_brands = list(s.exec(select(TrackedBrand)))
        run_q = select(AuditRunRecord)
        purchase_q = select(Purchase)
        if start_dt is not None:
            run_q = run_q.where(AuditRunRecord.started_at >= start_dt)
            purchase_q = purchase_q.where(Purchase.created_at >= start_dt)
        if end_dt is not None:
            run_q = run_q.where(AuditRunRecord.started_at < end_dt)
            purchase_q = purchase_q.where(Purchase.created_at < end_dt)
        all_runs = list(s.exec(run_q))
        all_purchases = list(s.exec(purchase_q))
        latest_runs_all = list(s.exec(select(AuditRunRecord)))

    brands_by_user: dict[str, list[TrackedBrand]] = {}
    for b in all_brands:
        brands_by_user.setdefault(str(b.user_id), []).append(b)
    runs_by_user: dict[str, list[AuditRunRecord]] = {}
    for r in all_runs:
        runs_by_user.setdefault(str(r.user_id), []).append(r)
    latest_by_user: dict[str, datetime] = {}
    for r in latest_runs_all:
        k = str(r.user_id)
        if k not in latest_by_user or r.started_at > latest_by_user[k]:
            latest_by_user[k] = r.started_at
    purchases_by_email: dict[str, list[Purchase]] = {}
    for p in all_purchases:
        purchases_by_email.setdefault((p.email or "").lower(), []).append(p)

    from src.server import TIER_PLANS
    rows: list[dict] = []
    for u in auth_users:
        brands = brands_by_user.get(u["id"], [])
        runs = runs_by_user.get(u["id"], [])
        purchases = purchases_by_email.get((u["email"] or "").lower(), [])
        active_sub_labels = "; ".join(
            f"{b.name} ({TIER_PLANS.get(b.tier, {}).get('label', b.tier)})"
            for b in brands if b.tier
        )
        rows.append({
            "user_id": u["id"],
            "email": u.get("email") or "",
            "is_master": "yes" if is_master_email(u.get("email") or "") else "",
            "email_confirmed": "yes" if u.get("email_confirmed_at") else "",
            "signed_up_utc": u["created_at"].strftime("%Y-%m-%d %H:%M:%S") if u.get("created_at") else "",
            "last_sign_in_utc": u["last_sign_in_at"].strftime("%Y-%m-%d %H:%M:%S") if u.get("last_sign_in_at") else "",
            "brand_count": len(brands),
            "run_count_in_range": len(runs),
            "purchase_count_in_range": len(purchases),
            "revenue_usd_in_range": round(sum(p.amount_usd or 0 for p in purchases), 2),
            "latest_run_all_time": latest_by_user[u["id"]].strftime("%Y-%m-%d %H:%M:%S") if u["id"] in latest_by_user else "",
            "active_subscriptions": active_sub_labels,
        })

    # Apply search + has-range filters identically to the HTML view.
    if q.strip():
        ql = q.strip().lower()
        rows = [r for r in rows if ql in (r.get("email") or "").lower()]
    if has_range:
        rows = [r for r in rows if r["run_count_in_range"] or r["purchase_count_in_range"]]
    rows.sort(
        key=lambda x: (x["revenue_usd_in_range"], x["run_count_in_range"]),
        reverse=True,
    )

    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    fieldnames = list(rows[0].keys()) if rows else [
        "user_id", "email", "is_master", "email_confirmed",
        "signed_up_utc", "last_sign_in_utc", "brand_count",
        "run_count_in_range", "purchase_count_in_range", "revenue_usd_in_range",
        "latest_run_all_time", "active_subscriptions",
    ]
    writer = _csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = buf.getvalue()

    # Filename embeds the active range so multiple exports don't overwrite
    # each other in the downloads folder.
    label = range or "all"
    if has_range and label == "custom":
        label = f"{start or 'start'}_to_{end or 'end'}"
    filename = f"monitoraeo-users-{label}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/users/{target_user_id}", response_class=HTMLResponse)
def admin_user_detail(
    request: Request, target_user_id: str, error: str = "",
):
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        target_uuid = UUID(target_user_id)
    except ValueError:
        raise HTTPException(404, "User not found")

    auth_users = _list_auth_users()
    target = next((u for u in auth_users if u["id"] == target_user_id), None)
    if not target:
        raise HTTPException(404, "User not found")

    from src.db import Purchase
    with get_session() as s:
        brands = list(
            s.exec(
                select(TrackedBrand)
                .where(TrackedBrand.user_id == target_uuid)
                .order_by(TrackedBrand.created_at.desc())
            )
        )
        runs = list(
            s.exec(
                select(AuditRunRecord)
                .where(AuditRunRecord.user_id == target_uuid)
                .order_by(AuditRunRecord.started_at.desc())
            )
        )
        purchases = list(
            s.exec(
                select(Purchase)
                .where(Purchase.email == (target["email"] or "").lower())
                .order_by(Purchase.created_at.desc())
            )
        )
    from src.server import TIER_PLANS
    revenue_usd = round(sum(p.amount_usd or 0 for p in purchases), 2)
    purchases_view = [
        {
            "created_at": p.created_at,
            "tier": p.tier,
            "tier_short": _short_tier(p.tier),
            "tier_label": TIER_PLANS.get(p.tier, {}).get("label", p.tier),
            "amount_usd": p.amount_usd,
            "brand_name": p.brand_name,
            "domain": p.domain,
            "kind": p.kind,
        }
        for p in purchases
    ]
    return _render(
        "admin_user_detail.html.j2",
        user=user,
        target=target,
        brands=brands,
        runs=runs[:25],
        run_total=len(runs),
        purchases=purchases_view,
        revenue_usd=revenue_usd,
        tier_plans=TIER_PLANS,
        is_self=(target_user_id == user["id"]),
        flash_error=error or None,
        active_tab="admin",
    )


@router.post("/admin/users/{target_user_id}/delete")
def admin_user_delete(request: Request, target_user_id: str):
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user
    if target_user_id == user["id"]:
        return RedirectResponse(
            f"/dashboard/admin/users/{target_user_id}?error=cant_delete_self",
            status_code=303,
        )
    try:
        target_uuid = UUID(target_user_id)
    except ValueError:
        raise HTTPException(404, "User not found")

    # Cascade-clean our public tables before asking Supabase to remove the
    # auth.users row (FK-free, but we want a clean state regardless). We
    # deliberately keep Purchase rows so deleting a customer doesn't make
    # historical revenue evaporate from accounting.
    from src.db import TeamInvite
    with get_session() as s:
        for r in s.exec(
            select(AuditRunRecord).where(AuditRunRecord.user_id == target_uuid)
        ):
            s.delete(r)
        for b in s.exec(
            select(TrackedBrand).where(TrackedBrand.user_id == target_uuid)
        ):
            s.delete(b)
        for inv in s.exec(
            select(TeamInvite).where(TeamInvite.owner_user_id == target_uuid)
        ):
            s.delete(inv)
        s.commit()

    # Drop the auth.users row via the Supabase admin API. Requires
    # SUPABASE_SERVICE_ROLE_KEY (separate from the anon key) — without it
    # we still wipe the public-schema rows but the auth row stays.
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    if service_key and supabase_url:
        try:
            from supabase import create_client  # type: ignore[import-not-found]
            admin_client = create_client(supabase_url, service_key)
            admin_client.auth.admin.delete_user(target_user_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[admin] supabase delete_user failed: {type(exc).__name__}: {exc}")
            return RedirectResponse(
                "/dashboard/admin?error=auth_delete_failed", status_code=303
            )
    else:
        return RedirectResponse(
            "/dashboard/admin?error=missing_service_key", status_code=303
        )
    return RedirectResponse("/dashboard/admin?deleted=1", status_code=303)


# ---------------------------------------------------------------------------
# Admin — re-fulfil an orphaned Stripe checkout session.
# Used to manually recover paid orders whose post-payment flow failed
# (e.g. the stripe-python 8+ StripeObject crash). Looks up the session
# from Stripe, rebuilds the meta the way the live flow does, splices in
# any competitors the admin typed, and kicks off _fulfil_order in a
# background thread. Same idempotency as the live path — the audit
# only re-runs if it wasn't completed before.
# ---------------------------------------------------------------------------

_REFULFIL_FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>Refulfil — monitoraeo admin</title>
<style>body{{font-family:-apple-system,Inter,sans-serif; max-width:640px;
margin:60px auto; padding:0 24px; color:#0f172a;}}
h1{{font-size:22px; margin:0 0 6px;}}
p{{color:#475569; line-height:1.55;}}
label{{display:block; font-size:12px; font-weight:800; text-transform:uppercase;
letter-spacing:.06em; color:#334155; margin:18px 0 6px;}}
input,textarea{{width:100%; padding:10px 12px; border:1px solid #e2e8f0;
border-radius:8px; font:inherit; font-size:14px; box-sizing:border-box;}}
button{{margin-top:18px; padding:12px 22px; border:0; border-radius:8px;
background:#2563eb; color:#fff; font-weight:700; cursor:pointer;}}
.flash{{padding:12px 14px; border-radius:8px; margin:18px 0; font-size:14px;
line-height:1.5;}}
.ok{{background:#dcfce7; color:#166534;}}
.err{{background:#fee2e2; color:#991b1b;}}
a{{color:#2563eb;}}
</style></head><body>
<p><a href="/dashboard/admin">← admin</a></p>
<h1>Re-fulfil orphaned Stripe order</h1>
<p>Paste a Stripe Checkout Session ID (the <code>cs_live_…</code> from the
<code>/checkout/success?session_id=…</code> URL) and the competitor list the
buyer would have typed. Kicks off the audit + creates the TrackedBrand and
AuditRunRecord for the buyer's email.</p>
{flash}
<form method="post" action="/dashboard/admin/refulfil">
  <label for="session_id">Stripe session ID</label>
  <input id="session_id" name="session_id" required placeholder="cs_live_…" value="{session_id}">
  <label for="competitors">Competitors (one per line, max 5)</label>
  <textarea id="competitors" name="competitors" rows="5" placeholder="Salesforce&#10;HubSpot">{competitors}</textarea>
  <button type="submit">Re-fulfil order</button>
</form>
</body></html>"""


def _refulfil_flash(status: str, detail: str = "") -> str:
    if status == "ok":
        return f'<div class="flash ok">Re-fulfilment kicked off. {detail}</div>'
    if status:
        return f'<div class="flash err">Failed: {status}. {detail}</div>'
    return ""


@router.get("/admin/refulfil", response_class=HTMLResponse)
def admin_refulfil_form(
    request: Request,
    session_id: str = "",
    status: str = "",
    detail: str = "",
):
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user
    return HTMLResponse(_REFULFIL_FORM.format(
        flash=_refulfil_flash(status, detail),
        session_id=session_id, competitors="",
    ))


@router.post("/admin/refulfil")
def admin_refulfil_submit(
    request: Request,
    session_id: str = Form(...),
    competitors: str = Form(""),
):
    import threading
    import stripe as _stripe
    from src.server import (
        PENDING_ORDERS, _to_plain_dict, _meta_with_email, _fulfil_order,
    )
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user

    sid = (session_id or "").strip()
    if not sid:
        return RedirectResponse(
            "/dashboard/admin/refulfil?status=missing_session_id",
            status_code=303,
        )
    if not _stripe.api_key:
        return RedirectResponse(
            "/dashboard/admin/refulfil?status=stripe_key_missing",
            status_code=303,
        )
    try:
        sess_dict = _to_plain_dict(_stripe.checkout.Session.retrieve(sid))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/dashboard/admin/refulfil?status=stripe_lookup_failed"
            f"&detail={type(exc).__name__}&session_id={sid}",
            status_code=303,
        )

    meta = _meta_with_email(sess_dict.get("metadata") or {}, sess_dict)
    if not (meta.get("email") or "").strip():
        return RedirectResponse(
            f"/dashboard/admin/refulfil?status=no_email_on_session&session_id={sid}",
            status_code=303,
        )
    if not (meta.get("tier") or "").strip():
        return RedirectResponse(
            f"/dashboard/admin/refulfil?status=no_tier_on_session&session_id={sid}",
            status_code=303,
        )

    # Splice competitors typed by the admin into the meta — same shape
    # /orders/setup uses.
    comp_list = [c.strip() for c in competitors.splitlines() if c.strip()][:5]
    meta["competitors"] = comp_list

    threading.Thread(target=_fulfil_order, args=(meta,), daemon=True).start()
    PENDING_ORDERS.pop(sid, None)

    return RedirectResponse(
        f"/dashboard/admin/refulfil?status=ok&session_id={sid}"
        f"&detail=Audit+kicked+off+for+{meta.get('email','')}.",
        status_code=303,
    )


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
    the same 40 brand-aware paid queries _generate_paid_queries produces.

    Every brand in the dashboard is a paid customer (free previews can't
    claim in any more), so the empty-monitored-queries fallback must be
    the full paid 40 — not the 8-query _generic_brand_queries set, which
    is what the dashboard used to silently use after orphan-recovery
    brands hydrated with no seeded queries. Symptom: re-running a paid
    brand only fired 8 questions and the action plan was thin/missing."""
    from src.models import Query
    texts = [q.strip() for q in (brand.monitored_queries or []) if q and q.strip()]
    if not texts:
        from src.server import _generate_paid_queries
        # brand.competitors is list[{"name", "domain"}] — pull names only.
        # _generate_paid_queries also normalises but extract here so the
        # log line above shows the actual competitor names not raw dicts.
        comps = [
            (c.get("name") or "").strip()
            for c in (brand.competitors or [])
            if isinstance(c, dict)
        ]
        comps = [c for c in comps if c]
        return _generate_paid_queries(brand.name, comps)
    return [Query(query=t, type=_classify_query(t)) for t in texts]


def _run_audit_for_brand(
    brand_id: str,
    run_record_id: str,
    *,
    engines_override: set[str] | None | str = "_no_override",
    force_full_report: bool = False,
) -> None:
    """Background worker. Loads the brand, runs the existing audit pipeline,
    persists headline metrics back to the AuditRunRecord row.
    `engines_override` lets master-account callers pick an engine set ad-hoc
    (None means "all engines", a set picks specific labels). The sentinel
    "_no_override" means fall back to the brand's tier.
    `force_full_report` flips LLM scoring + action plan on regardless of the
    brand's tier — used for master accounts so testing on a free brand still
    produces the same report a paying subscriber would see."""
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
            [ApifyEngineConfig(label="Google AI Mode")]
            if labels is None or "Google AI Mode" in labels
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
        # `apify_configs` is the legacy field name — picked up by the factory,
        # which returns SearchApiEngine by default (Apify only on rollback).
        from src.engines.factory import build_default_engine
        for cfg in apify_configs:
            engine_objs.append(
                build_default_engine(
                    country_code=brand_snapshot["country"],
                    language_code=brand_snapshot["language"],
                    label=cfg.label,
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
        # Run the AI-engine queries and the 15-check technical audit in
        # parallel. Tech audit is HTTP-only ($0) so we always include it —
        # this also closes the long-standing gap where monitoring re-runs
        # produced reports without the Technical foundations section.
        from src.tech_audit import run_for_domain_async as run_tech_audit_async

        async def _gather():
            return await asyncio.gather(
                run_audit(engine_objs, queries, run_dir),
                run_tech_audit_async(brand_snapshot["domain"]),
            )

        try:
            responses, tech = asyncio.run(_gather())
        except Exception:
            # If the parallel gather fails, fall back to engines-only so the
            # core report still ships.
            responses = asyncio.run(run_audit(engine_objs, queries, run_dir))
            tech = None

        # Every brand in the dashboard is a paid customer (free previews
        # can't claim into the dashboard any more), so action plan + LLM
        # scoring should run on EVERY re-run regardless of brand.tier.
        # Otherwise one-off subscribers (two_engine / full_audit), whose
        # brand row has tier='' so the monitoring cron doesn't pick them
        # up, would silently lose the action plan + LLM scoring after
        # their first paid audit even though they paid for both.
        # force_full_report kept as a no-op alias for master-account paths.
        from src.server import TIER_PLANS  # noqa: F401 — imported for parity
        want_llm = True
        want_plan = True

        llm_scores: list = []
        if want_llm:
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
        # subscriber sees every metric the paid one-shot audit produces.
        action_plan = None
        if want_plan:
            try:
                from src.action_plan import generate as generate_action_plan
                action_plan = generate_action_plan(rows, site)
            except Exception as exc:  # noqa: BLE001
                print(f"[audit] action_plan generation failed: {type(exc).__name__}: {exc}")
        # Log what we'll hand to write_html so we can tell from the deploy
        # logs whether the sidebar dimming on 'Action plan' is because we
        # generated nothing vs because the template skipped it on render.
        print(
            f"[audit] write_html action_plan="
            f"{'None' if action_plan is None else f'list len={len(action_plan)}'}"
        )
        write_html(
            rows,
            site,
            run_dir,
            tier="full",
            action_plan=action_plan,
            tech=tech,
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


# ---------------------------------------------------------------------------
# Admin — industry rankings (Play 2 programmatic SEO)
# Master-only: list, add, trigger immediate refresh of /ai-visibility/* pages.
# ---------------------------------------------------------------------------

_INDUSTRY_ADMIN_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Industry rankings · admin</title>
<style>
  body {{ font-family: Inter, system-ui, sans-serif; margin: 32px; max-width: 1100px; color: #0f172a; }}
  h1 {{ font-size: 28px; letter-spacing: -.03em; }}
  h2 {{ font-size: 20px; margin-top: 32px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px; }}
  th, td {{ padding: 10px 12px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
  th {{ background: #f8fafc; font-weight: 800; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }}
  form {{ margin: 16px 0; padding: 20px; background: #f8fafc; border-radius: 12px; border: 1px solid #e2e8f0; }}
  label {{ display: block; font-weight: 700; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: #64748b; margin-bottom: 4px; }}
  input, textarea, select {{ width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #cbd5e1; font: inherit; margin-bottom: 12px; }}
  textarea {{ min-height: 120px; font-family: ui-monospace, Menlo, monospace; font-size: 13px; }}
  button {{ padding: 10px 18px; border-radius: 999px; border: 0; background: linear-gradient(135deg, #2563eb, #7c3aed); color: white; font-weight: 800; cursor: pointer; }}
  .pill {{ display: inline-flex; align-items: center; padding: 3px 9px; border-radius: 999px; background: #eff6ff; color: #1d4ed8; font-size: 11px; font-weight: 800; }}
  .pill.warn {{ background: #fef3c7; color: #92400e; }}
  .flash {{ padding: 12px 16px; background: #ecfdf5; border: 1px solid #86efac; color: #166534; border-radius: 12px; margin-bottom: 16px; }}
  .flash.err {{ background: #fef2f2; border-color: #fca5a5; color: #991b1b; }}
  code {{ font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; background: #f1f5f9; padding: 2px 6px; border-radius: 6px; }}
</style>
<h1>Industry rankings · admin</h1>
<p><a href="/dashboard/admin">← back to admin</a> · <a href="/ai-visibility" target="_blank">view public index ↗</a></p>
{flash}

<h2>Add a new industry</h2>
<form method="POST" action="/dashboard/admin/industries">
  <label>Slug (URL fragment, lowercase-hyphenated)</label>
  <input name="slug" required pattern="[a-z0-9-]+" placeholder="e.g. crm-software">

  <label>Display name</label>
  <input name="name" required placeholder="e.g. CRM software">

  <label>Parent category</label>
  <select name="parent_category" required>
    {category_options}
  </select>

  <label>Short description (1 sentence, shown on page lede)</label>
  <input name="description" placeholder="e.g. Customer relationship management platforms used by sales and support teams.">

  <label>Brand list (one per line, format <code>Brand Name | domain.com</code>)</label>
  <textarea name="brands" required placeholder="HubSpot | hubspot.com
Salesforce | salesforce.com
Pipedrive | pipedrive.com
Zoho CRM | zoho.com
..."></textarea>

  <button type="submit">Create industry + queue audit →</button>
</form>

<h2>Existing rankings</h2>
<form method="POST" action="/dashboard/admin/industries/refresh-all" style="margin:0 0 12px; padding:0; background:transparent; border:0; display:inline-block;"
      onsubmit="return confirm('Queue every industry for cron refresh? Each takes ~60-90s of Apify time. The cron will process 3 per 5 min until they\\'re all updated.');">
  <button type="submit" style="padding:8px 14px; font-size:13px; background: linear-gradient(135deg, #16a34a, #059669); border: 0; border-radius: 999px; color: white; font-weight: 800; cursor: pointer;">Refresh ALL industries now</button>
</form>
{rows_html}

<h2>Programmatic API</h2>
<p>Same operations as this page, but as JSON endpoints. Set <code>INDUSTRY_API_TOKEN</code> on Railway then use the bearer token in the <code>Authorization</code> header. <strong>Do not share the token publicly.</strong></p>

<details open style="padding:16px 20px; background:#0b1220; color:#cbd5e1; border-radius:12px; border:1px solid #1e293b;">
  <summary style="cursor:pointer; font-weight:800; color:white; font-size:14px;">Create a new industry (POST)</summary>
<pre style="margin:12px 0 0; overflow-x:auto; font-size:12.5px; line-height:1.55;">curl -X POST https://www.monitoraeo.com/api/industries \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "slug": "crm-software",
    "name": "CRM software",
    "parent_category": "SaaS",
    "description": "CRM platforms used by sales and support teams.",
    "brands": [
      {{"name": "HubSpot",    "domain": "hubspot.com"}},
      {{"name": "Salesforce", "domain": "salesforce.com"}},
      {{"name": "Pipedrive",  "domain": "pipedrive.com"}}
    ],
    "refresh_immediately": true
  }}'</pre>
  <p style="margin:10px 0 0; color:#94a3b8; font-size:12.5px;">Returns 201 with the public URL and refresh schedule. 409 if the slug already exists. Cron will pick up the new industry within ~5 minutes when <code>refresh_immediately</code> is true.</p>
</details>

<details style="padding:16px 20px; background:#0b1220; color:#cbd5e1; border-radius:12px; border:1px solid #1e293b; margin-top:10px;">
  <summary style="cursor:pointer; font-weight:800; color:white; font-size:14px;">Trigger an immediate refresh (POST)</summary>
<pre style="margin:12px 0 0; overflow-x:auto; font-size:12.5px; line-height:1.55;">curl -X POST https://www.monitoraeo.com/api/industries/crm-software/refresh \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN"</pre>
  <p style="margin:10px 0 0; color:#94a3b8; font-size:12.5px;">Synchronous — returns the summary when the audit completes (~30-90s). Use this after creating to skip the cron wait, or for one-off forced refreshes.</p>
</details>

<details style="padding:16px 20px; background:#0b1220; color:#cbd5e1; border-radius:12px; border:1px solid #1e293b; margin-top:10px;">
  <summary style="cursor:pointer; font-weight:800; color:white; font-size:14px;">List all industries (GET)</summary>
<pre style="margin:12px 0 0; overflow-x:auto; font-size:12.5px; line-height:1.55;">curl https://www.monitoraeo.com/api/industries \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN"</pre>
</details>

<details style="padding:16px 20px; background:#0b1220; color:#cbd5e1; border-radius:12px; border:1px solid #1e293b; margin-top:10px;">
  <summary style="cursor:pointer; font-weight:800; color:white; font-size:14px;">Get one industry with its full ranking (GET)</summary>
<pre style="margin:12px 0 0; overflow-x:auto; font-size:12.5px; line-height:1.55;">curl https://www.monitoraeo.com/api/industries/crm-software \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN"</pre>
  <p style="margin:10px 0 0; color:#94a3b8; font-size:12.5px;">Returns the same data the public /ai-visibility/crm-software page renders from, in JSON form. Good for programmatic monitoring of ranking changes.</p>
</details>

<details style="padding:16px 20px; background:#0b1220; color:#cbd5e1; border-radius:12px; border:1px solid #1e293b; margin-top:10px;">
  <summary style="cursor:pointer; font-weight:800; color:white; font-size:14px;">Python example (requests)</summary>
<pre style="margin:12px 0 0; overflow-x:auto; font-size:12.5px; line-height:1.55;">import os, requests

API = "https://www.monitoraeo.com/api/industries"
TOKEN = os.environ["INDUSTRY_API_TOKEN"]
HEADERS = {{"Authorization": f"Bearer {{TOKEN}}"}}

# Create
r = requests.post(API, headers=HEADERS, json={{
    "slug": "crm-software",
    "name": "CRM software",
    "parent_category": "SaaS",
    "description": "CRM platforms used by sales and support teams.",
    "brands": [
        {{"name": "HubSpot",    "domain": "hubspot.com"}},
        {{"name": "Salesforce", "domain": "salesforce.com"}},
    ],
    "refresh_immediately": True,
}})
print(r.status_code, r.json())

# Optional: wait for first audit + fetch results
import time; time.sleep(120)
r = requests.get(f"{{API}}/crm-software", headers=HEADERS)
for b in r.json()["brands"]:
    print(f"{{b['rank']:>2}}. {{b['name']:<25}} visibility={{b['visibility_pct']:>5.1f}}%")</pre>
</details>

<p style="margin-top:14px; color:#64748b; font-size:12.5px;">Status codes: <code>201</code> created · <code>400</code> bad request · <code>401</code> bad token · <code>404</code> slug not found · <code>409</code> slug already exists · <code>500</code> server error · <code>503</code> token not configured on server (set <code>INDUSTRY_API_TOKEN</code>).</p>
"""


# ---------------------------------------------------------------------------
# Glossary admin — same shape as industries admin but for /glossary/{slug}
# DB-backed definitional pages. Master-only. Includes inline API docs.
# ---------------------------------------------------------------------------

_GLOSSARY_ADMIN_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Glossary pages · admin</title>
<style>
  body {{ font-family: Inter, system-ui, sans-serif; margin: 32px; max-width: 1100px; color: #0f172a; }}
  h1 {{ font-size: 28px; letter-spacing: -.03em; }}
  h2 {{ font-size: 20px; margin-top: 32px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px; }}
  th, td {{ padding: 10px 12px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
  th {{ background: #f8fafc; font-weight: 800; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }}
  code {{ font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; background: #f1f5f9; padding: 2px 6px; border-radius: 6px; }}
  .flash {{ padding: 12px 16px; background: #ecfdf5; border: 1px solid #86efac; color: #166534; border-radius: 12px; margin-bottom: 16px; }}
  .flash.err {{ background: #fef2f2; border-color: #fca5a5; color: #991b1b; }}
  .pill {{ display: inline-flex; align-items: center; padding: 3px 9px; border-radius: 999px; background: #eff6ff; color: #1d4ed8; font-size: 11px; font-weight: 800; }}
  .pill.warn {{ background: #fef3c7; color: #92400e; }}
  details pre {{ margin: 12px 0 0; overflow-x: auto; font-size: 12.5px; line-height: 1.55; }}
</style>
<h1>Glossary pages · admin</h1>
<p><a href="/dashboard/admin">← back to admin</a> · <a href="/glossary" target="_blank">view public glossary ↗</a></p>
{flash}

<p>This page lists every DB-backed glossary entry (rendered at <code>/glossary/&#123;slug&#125;</code>). Custom Jinja pages (<code>/what-is-aeo</code>, <code>/what-is-ai-mode</code> etc.) are not managed here — they live in the codebase as individual templates.</p>

<h2>Existing pages</h2>
{rows_html}

<h2>Programmatic API</h2>
<p>Use the bearer token in <code>INDUSTRY_API_TOKEN</code> (same token as the industries API — single secret to manage). Hand the section below to a contractor or use it yourself.</p>

<details open style="padding:16px 20px; background:#0b1220; color:#cbd5e1; border-radius:12px; border:1px solid #1e293b;">
  <summary style="cursor:pointer; font-weight:800; color:white; font-size:14px;">Create a new glossary page (POST)</summary>
<pre>curl -X POST https://www.monitoraeo.com/api/definitional-pages \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "slug": "share-of-voice-in-ai-answers",
    "name": "Share of voice in AI answers",
    "parent_section": "Metrics",
    "target_kw": "ai share of voice",
    "short_definition": "Your brand visibility relative to competitors in the same AI answer set.",
    "meta_description": "Share of voice in AI answers is how often your brand is named relative to competitors in the same set of AI responses. The most defensible AEO metric — it normalises for query selection bias.",
    "lede": "Share of voice in AI answers is your brand visibility relative to the other brands the AI mentions in the same answer set. The most defensible AEO metric because it normalises for query selection bias.",
    "sections": [
      {{
        "heading": "How it is calculated",
        "body_html": "<p>For a given query set: <code>share_of_voice = your_mentions / total_brand_mentions_across_all_brands_in_the_set</code>. Expressed as a percentage. Sums to 100 across all brands measured in the same set.</p><p>The denominator is the key — small denominator means small set, big swings.</p>"
      }},
      {{
        "heading": "Why share of voice beats raw visibility",
        "body_html": "<p>Raw visibility (% of answers naming your brand) shifts based on which questions you ask. Add a softball brand-name question and visibility jumps; add a hard category question and it drops. Share of voice normalises — it asks <em>across the same set</em>, what share did you win?</p>"
      }}
    ],
    "faqs": [
      {{"q": "How is share of voice different from visibility?",
        "a": "Visibility is absolute (% of answers naming your brand). Share of voice is relative (your share of brand mentions across all brands in the same answer set)."}}
    ],
    "related_slugs": ["visibility-metric", "citation-rate-meaning", "/what-is-aeo"],
    "alternate_names": ["SoV in AI", "AI share of voice"]
  }}'</pre>
</details>

<details style="padding:16px 20px; background:#0b1220; color:#cbd5e1; border-radius:12px; border:1px solid #1e293b; margin-top:10px;">
  <summary style="cursor:pointer; font-weight:800; color:white; font-size:14px;">Update an existing page (PATCH)</summary>
<pre>curl -X PATCH https://www.monitoraeo.com/api/definitional-pages/share-of-voice-in-ai-answers \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"lede": "Updated lede sentence."}}'</pre>
  <p style="margin:10px 0 0; color:#94a3b8; font-size:12.5px;">Only fields you include are updated. Omitted fields keep their existing values. <code>updated_at</code> is auto-bumped so sitemap <code>lastmod</code> + Article <code>dateModified</code> refresh.</p>
</details>

<details style="padding:16px 20px; background:#0b1220; color:#cbd5e1; border-radius:12px; border:1px solid #1e293b; margin-top:10px;">
  <summary style="cursor:pointer; font-weight:800; color:white; font-size:14px;">List + get + delete</summary>
<pre>curl https://www.monitoraeo.com/api/definitional-pages \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN"

curl https://www.monitoraeo.com/api/definitional-pages/share-of-voice-in-ai-answers \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN"

curl -X DELETE https://www.monitoraeo.com/api/definitional-pages/share-of-voice-in-ai-answers \\
  -H "Authorization: Bearer $INDUSTRY_API_TOKEN"</pre>
</details>

<h3 style="margin-top:24px;">Contractor brief — required content fields</h3>
<table>
  <thead><tr><th>Field</th><th>Purpose</th><th>Constraints</th></tr></thead>
  <tbody>
    <tr><td><code>slug</code></td><td>URL fragment: <code>/glossary/&#123;slug&#125;</code></td><td>Lowercase alphanumeric + hyphens. Unique.</td></tr>
    <tr><td><code>name</code></td><td>Page H1 + title</td><td>Title-case. Concise.</td></tr>
    <tr><td><code>parent_section</code></td><td>Which section on /glossary lists this entry</td><td>One of: <code>Concepts</code>, <code>Google AI surfaces</code>, <code>Engines</code>, <code>Metrics</code>, <code>Tactics</code></td></tr>
    <tr><td><code>target_kw</code></td><td>Primary keyword (notes only, not rendered)</td><td>Lowercase. Used as a reminder of what the page targets.</td></tr>
    <tr><td><code>short_definition</code></td><td>Glossary index card description</td><td>~30 words. Answer "what is X" in one breath.</td></tr>
    <tr><td><code>meta_description</code></td><td><code>&lt;meta name=description&gt;</code></td><td>~155 chars. Falls back to <code>short_definition</code> if empty.</td></tr>
    <tr><td><code>lede</code></td><td>Paragraph under the H1</td><td>~50 words. Direct answer to the target_kw. AI engines extract this.</td></tr>
    <tr><td><code>sections</code></td><td>Body content</td><td>List of <code>{{heading, body_html}}</code>. body_html supports any standard HTML.</td></tr>
    <tr><td><code>faqs</code></td><td>FAQPage schema + visible FAQ block</td><td>5–7 entries. <code>{{q, a}}</code> objects. Plain text only in <code>a</code>.</td></tr>
    <tr><td><code>related_slugs</code></td><td>Cross-link footer</td><td>4–6 entries. Either bare slugs (<code>"visibility-metric"</code> → /glossary/visibility-metric) or absolute paths (<code>"/what-is-aeo"</code>).</td></tr>
    <tr><td><code>alternate_names</code></td><td>DefinedTerm.alternateName schema</td><td>Synonyms / acronyms only. Optional.</td></tr>
  </tbody>
</table>

<p style="margin-top:14px; color:#64748b; font-size:12.5px;">Status codes: <code>201</code> created · <code>200</code> updated · <code>400</code> bad request · <code>401</code> bad token · <code>404</code> slug not found · <code>409</code> slug already exists · <code>500</code> server error · <code>503</code> <code>INDUSTRY_API_TOKEN</code> not set on server.</p>
"""


def _glossary_admin_render(flash: str = "") -> str:
    from sqlmodel import select as _select
    from src.db import DefinitionalPage, get_session
    rows_html = ""
    try:
        with get_session() as s:
            pages = list(s.exec(_select(DefinitionalPage).order_by(DefinitionalPage.parent_section, DefinitionalPage.name)))
            if not pages:
                rows_html = "<p><em>No DB-backed glossary pages yet. Create one via the API below — they appear at <code>/glossary/{slug}</code>.</em></p>"
            else:
                rows_html = (
                    "<table><thead><tr>"
                    "<th>Slug</th><th>Name</th><th>Section</th>"
                    "<th>Sections</th><th>FAQs</th><th>Updated</th><th></th>"
                    "</tr></thead><tbody>"
                )
                for p in pages:
                    upd = p.updated_at.strftime("%Y-%m-%d") if p.updated_at else "—"
                    rows_html += (
                        f"<tr>"
                        f"<td><code>{p.slug}</code></td>"
                        f"<td><a href='/glossary/{p.slug}' target='_blank'>{p.name}</a></td>"
                        f"<td>{p.parent_section or '—'}</td>"
                        f"<td>{len(p.sections or [])}</td>"
                        f"<td>{len(p.faqs or [])}</td>"
                        f"<td>{upd}</td>"
                        f"<td><a href='/api/definitional-pages/{p.slug}' target='_blank' style='font-size:12px;'>JSON</a></td>"
                        f"</tr>"
                    )
                rows_html += "</tbody></table>"
    except Exception as exc:  # noqa: BLE001
        rows_html = f"<p class='flash err'>DB unavailable: {type(exc).__name__}: {exc}</p>"
    return _GLOSSARY_ADMIN_HTML.format(flash=flash, rows_html=rows_html)


@router.get("/admin/glossary", response_class=HTMLResponse)
def admin_glossary(request: Request):
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user
    return HTMLResponse(_glossary_admin_render())


def _industries_admin_render(flash: str = "") -> str:
    """Render the admin page with the current industry list. Pulled out so
    GET and POST handlers share the same render path."""
    from sqlmodel import select as _select, func as _func
    from src.db import IndustryReport, IndustryBrand, get_session
    from src.server import INDUSTRY_PARENT_CATEGORIES

    category_options = "\n".join(
        f'<option value="{c}">{c}</option>' for c in INDUSTRY_PARENT_CATEGORIES
    )

    rows_html = ""
    try:
        with get_session() as s:
            reports = list(s.exec(_select(IndustryReport).order_by(IndustryReport.created_at.desc())))
            if not reports:
                rows_html = "<p><em>No industries published yet.</em></p>"
            else:
                rows_html = (
                    "<table><thead><tr>"
                    "<th>Slug</th><th>Name</th><th>Category</th>"
                    "<th>Brands</th><th>Last refresh</th><th>Next refresh</th><th></th>"
                    "</tr></thead><tbody>"
                )
                for r in reports:
                    brand_count = s.exec(
                        _select(_func.count(IndustryBrand.id))
                        .where(IndustryBrand.industry_slug == r.slug)
                    ).one() or 0
                    last = r.last_full_refresh.strftime("%Y-%m-%d %H:%M") if r.last_full_refresh else "<span class='pill warn'>never</span>"
                    nxt = r.next_scheduled_refresh.strftime("%Y-%m-%d") if r.next_scheduled_refresh else "<span class='pill warn'>asap</span>"
                    rows_html += (
                        f"<tr>"
                        f"<td><code>{r.slug}</code></td>"
                        f"<td><a href='/ai-visibility/{r.slug}' target='_blank'>{r.name}</a></td>"
                        f"<td>{r.parent_category or '—'}</td>"
                        f"<td>{int(brand_count)}</td>"
                        f"<td>{last}</td>"
                        f"<td>{nxt}</td>"
                        f"<td><form method='POST' action='/dashboard/admin/industries/{r.slug}/refresh' style='display:inline; margin:0; padding:0; background:transparent; border:0;'>"
                        f"<button type='submit' style='padding:6px 12px; font-size:12px;'>Refresh now</button></form></td>"
                        f"</tr>"
                    )
                rows_html += "</tbody></table>"
    except Exception as exc:  # noqa: BLE001
        rows_html = f"<p class='flash err'>DB unavailable: {type(exc).__name__}: {exc}</p>"

    return _INDUSTRY_ADMIN_HTML.format(
        flash=flash, category_options=category_options, rows_html=rows_html,
    )


@router.get("/admin/industries", response_class=HTMLResponse)
def admin_industries(request: Request, status: str = "", detail: str = ""):
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user
    flash = ""
    if status == "created":
        flash = f"<div class='flash'>✓ Industry <code>{detail}</code> created. Cron will refresh it on next tick.</div>"
    elif status == "refresh_queued":
        flash = f"<div class='flash'>✓ <code>{detail}</code> queued for immediate refresh.</div>"
    elif status == "refresh_all_queued":
        flash = f"<div class='flash'>✓ Queued all <strong>{detail}</strong> industries for refresh. Cron will process them at 3 per ~5 min over the next hour or two.</div>"
    elif status == "refresh_all_failed":
        flash = f"<div class='flash err'>Refresh-all failed: {detail}. Check the Railway logs.</div>"
    elif status:
        flash = f"<div class='flash err'>Error: {status} — {detail}</div>"
    return HTMLResponse(_industries_admin_render(flash=flash))


@router.post("/admin/industries")
def admin_industries_create(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    parent_category: str = Form(""),
    description: str = Form(""),
    brands: str = Form(...),
):
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user

    from sqlmodel import select as _select
    from src.db import IndustryReport, IndustryBrand, get_session

    slug = re.sub(r"[^a-z0-9-]", "", slug.strip().lower())
    if not slug or not name.strip():
        return RedirectResponse(
            "/dashboard/admin/industries?status=invalid&detail=slug+and+name+required",
            status_code=303,
        )

    # Parse brand lines: "Brand Name | domain.com".
    # IMPORTANT: use explicit startswith/slice rather than str.lstrip(prefix) —
    # str.lstrip treats its argument as a SET of characters to strip, not a
    # prefix string, which eats the first letter of any domain that happens
    # to start with h/t/p/s (e.g. hubspot.com -> ubspot.com).
    brand_rows: list[tuple[str, str]] = []
    for line in (brands or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            continue
        dom = parts[1].lower()
        for prefix in ("https://", "http://"):
            if dom.startswith(prefix):
                dom = dom[len(prefix):]
        if dom.startswith("www."):
            dom = dom[4:]
        dom = dom.rstrip("/")
        if dom:
            brand_rows.append((parts[0], dom))
    if not brand_rows:
        return RedirectResponse(
            "/dashboard/admin/industries?status=invalid&detail=no+valid+brand+lines",
            status_code=303,
        )

    try:
        with get_session() as s:
            existing = s.exec(_select(IndustryReport).where(IndustryReport.slug == slug)).first()
            if existing:
                return RedirectResponse(
                    f"/dashboard/admin/industries?status=duplicate&detail={slug}",
                    status_code=303,
                )
            report = IndustryReport(
                slug=slug,
                name=name.strip(),
                parent_category=parent_category.strip(),
                description=description.strip(),
                next_scheduled_refresh=datetime.utcnow(),  # eligible immediately
            )
            s.add(report)
            for brand_name, brand_domain in brand_rows:
                s.add(IndustryBrand(
                    industry_slug=slug,
                    brand_name=brand_name,
                    brand_domain=brand_domain,
                ))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/dashboard/admin/industries?status=db_error&detail={type(exc).__name__}",
            status_code=303,
        )

    try:
        from src.server import invalidate_ai_visibility_cache
        invalidate_ai_visibility_cache()
    except Exception:  # noqa: BLE001
        pass
    return RedirectResponse(
        f"/dashboard/admin/industries?status=created&detail={slug}",
        status_code=303,
    )


@router.post("/admin/industries/{slug}/refresh")
def admin_industries_refresh(request: Request, slug: str):
    """Trigger an immediate refresh of one industry's rankings, off-thread.
    The cron will pick it up on its next tick if this fails."""
    import threading as _threading
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user

    def _run():
        try:
            from src.industry_audit import refresh_industry
            summary = refresh_industry(slug)
            print(f"[admin] manual refresh {slug}: {summary}")
            try:
                from src.server import invalidate_ai_visibility_cache
                invalidate_ai_visibility_cache()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            print(f"[admin] manual refresh {slug} raised: {type(exc).__name__}: {exc}")

    _threading.Thread(target=_run, daemon=True).start()
    return RedirectResponse(
        f"/dashboard/admin/industries?status=refresh_queued&detail={slug}",
        status_code=303,
    )


@router.post("/admin/industries/refresh-all")
def admin_industries_refresh_all(request: Request):
    """Queue every existing industry for immediate cron pickup. Bumps
    next_scheduled_refresh = now for all rows; the cron worker processes
    INDUSTRY_BATCH_SIZE per CHECK_INTERVAL tick (default 3 per 5 min) so
    a large set drains over the following hour or two without spiking
    Apify load. Idempotent — calling twice just resets the same field
    to a slightly newer 'now'."""
    user = _require_master(request)
    if isinstance(user, RedirectResponse):
        return user

    try:
        from src.db import IndustryReport, get_session
        from sqlmodel import select as _select
        with get_session() as s:
            count = 0
            now = datetime.utcnow()
            for r in s.exec(_select(IndustryReport)):
                r.next_scheduled_refresh = now
                s.add(r)
                count += 1
            s.commit()
        try:
            from src.server import invalidate_ai_visibility_cache
            invalidate_ai_visibility_cache()
        except Exception:  # noqa: BLE001
            pass
        return RedirectResponse(
            f"/dashboard/admin/industries?status=refresh_all_queued&detail={count}",
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/dashboard/admin/industries?status=refresh_all_failed&detail={type(exc).__name__}",
            status_code=303,
        )
