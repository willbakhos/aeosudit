"""Email delivery via Resend. Sends the buyer a link into the dashboard
when their report is ready, and a 'welcome' confirmation right after payment."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import resend

TIER_LABELS = {
    "two_engine": "Two Engine Audit ($29)",
    "full_audit": "Full Audit ($79)",
    "two_engine_monthly": "Two Engine Monitoring ($35/mo)",
    "full_monthly": "Full Monitoring ($95/mo)",
}


# Solid-background button — gradients are stripped by most email clients, so
# the old gradient buttons rendered as transparent boxes with invisible text.
# Square-ish radius + !important on color survives dark-mode auto-invert.
def _button(href: str, label: str) -> str:
    return (
        f'<a href="{href}" style="'
        'display:inline-block; padding:14px 28px; background-color:#2563eb;'
        'color:#ffffff !important; text-decoration:none; font-weight:700;'
        'font-size:15px; line-height:1; border-radius:8px;'
        'font-family:-apple-system,Inter,sans-serif;'
        f'">{label}</a>'
    )


def _build_html(brand_name: str, tier: str, dashboard_url: str, has_hero: bool) -> str:
    tier_label = TIER_LABELS.get(tier, tier)
    hero_block = (
        '<img src="cid:report_hero" alt="" '
        'style="display:block; width:100%; max-width:560px; height:auto; '
        'border-radius:14px; margin:0 0 20px;">'
        if has_hero else ""
    )
    return f"""
    <div style="font-family:-apple-system,Inter,sans-serif; max-width:560px; margin:0 auto; color:#0f172a;">
      {hero_block}
      <h2 style="font-size:22px; margin:0 0 12px;">Your {brand_name} AEO audit is ready</h2>
      <p style="color:#475569; line-height:1.55;">
        Tier: <strong>{tier_label}</strong>
      </p>
      <p style="color:#475569; line-height:1.55;">
        We tested how AI answer engines describe, cite and compare {brand_name}.
        Sign in to your dashboard to open the full report.
      </p>
      <p style="margin:24px 0;">
        {_button(dashboard_url, "Open my dashboard →")}
      </p>
      <p style="color:#94a3b8; font-size:12px; line-height:1.5;">
        Questions? Just reply to this email.
      </p>
    </div>
    """


def send_report(
    *,
    to_email: str,
    brand_name: str,
    tier: str,
    dashboard_url: str,
    hero_image_path: Path | None = None,
) -> dict[str, Any]:
    """Send the 'your audit is ready' email. Returns the Resend response dict.
    Raises if RESEND_API_KEY is missing.

    hero_image_path: optional 1200x630 PNG (from src.teaser_image.generate)
    that gets embedded at the top of the email body via CID attachment."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set")
    resend.api_key = api_key

    from_addr = os.environ.get("REPORT_FROM_EMAIL", "monitoraeo <reports@monitoraeo.com>")
    subject = f"Your {brand_name} AEO audit is ready"
    has_hero = bool(hero_image_path and hero_image_path.exists())
    html = _build_html(brand_name, tier, dashboard_url, has_hero=has_hero)

    attachments: list[dict[str, Any]] = []
    if has_hero:
        attachments.append({
            "filename": "hero.png",
            "content": base64.b64encode(hero_image_path.read_bytes()).decode("ascii"),
            "content_id": "report_hero",
            "disposition": "inline",
        })

    params: dict[str, Any] = {
        "from": from_addr,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if attachments:
        params["attachments"] = attachments

    return resend.Emails.send(params)


def send_industry_pdf(
    *,
    to_email: str,
    industry_name: str,
    industry_url: str,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> dict[str, Any]:
    """Send an email-gated download of the /ai-visibility/{slug} PDF.
    Returns the Resend response dict. Raises if RESEND_API_KEY is missing.

    industry_url: canonical URL of the public ranking page (linked from the
        email so the recipient can revisit the live page).
    pdf_filename: download name shown in the recipient's mail client
        (e.g. 'monitoraeo-crm-software-rankings.pdf')."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set")
    resend.api_key = api_key

    from_addr = os.environ.get("REPORT_FROM_EMAIL", "monitoraeo <reports@monitoraeo.com>")
    subject = f"Your AI visibility ranking PDF: {industry_name}"
    html = f"""\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  max-width:560px; margin:0 auto; padding:32px 24px; color:#0f172a; line-height:1.55;">
  <h1 style="font-size:22px; font-weight:800; letter-spacing:-.02em; margin:0 0 16px;">
    Your <strong>{_html_escape(industry_name)}</strong> ranking PDF
  </h1>
  <p style="margin:0 0 14px; font-size:15px;">
    The PDF is attached. It captures the current AI visibility rankings,
    citation rates, top cited sources, and the methodology behind the scores.
  </p>
  <p style="margin:0 0 24px; font-size:15px;">
    View the live page (refreshed monthly):
    <a href="{_html_escape(industry_url)}" style="color:#2563eb; font-weight:700;">{_html_escape(industry_url)}</a>
  </p>
  <div style="padding:18px 20px; border-radius:14px; background:#f8fafc; border:1px solid #e2e8f0; margin-bottom:24px;">
    <strong style="font-size:14px; display:block; margin-bottom:6px;">Want this for your own brand?</strong>
    <span style="font-size:14px; color:#475569;">
      Run a full AEO audit across 5 AI engines —
      <a href="https://www.monitoraeo.com/product/audit" style="color:#2563eb; font-weight:700;">monitoraeo.com/product/audit</a>
    </span>
  </div>
  <p style="font-size:12px; color:#94a3b8; margin:32px 0 0;">
    You're getting this because you requested it at monitoraeo.com.
    No further emails. Unsubscribe N/A — single send.
  </p>
</body></html>"""

    params: dict[str, Any] = {
        "from": from_addr,
        "to": [to_email],
        "subject": subject,
        "html": html,
        "attachments": [{
            "filename": pdf_filename,
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
        }],
    }
    return resend.Emails.send(params)


def _html_escape(s: str) -> str:
    """Minimal HTML escape for values that go into our email bodies."""
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def _build_welcome_html(
    *, brand_name: str, tier: str, is_subscription: bool, dashboard_url: str
) -> str:
    tier_label = TIER_LABELS.get(tier, tier)
    brand_line = f"<strong>{brand_name}</strong>" if brand_name else "your brand"
    recurring_block = (
        f"""
      <h3 style="font-size:14px; text-transform:uppercase; letter-spacing:.06em;
                 color:#475569; margin:28px 0 8px;">Recurring billing</h3>
      <p style="color:#475569; line-height:1.55; margin:0;">
        You're now subscribed on the <strong>{tier_label}</strong> plan. We'll re-run
        the audit on the 1st of each month and surface the results in your
        dashboard. Cancel any time from there.
      </p>
        """
        if is_subscription else ""
    )
    return f"""
    <div style="font-family:-apple-system,Inter,sans-serif; max-width:560px; margin:0 auto; color:#0f172a;">
      <h2 style="font-size:22px; margin:0 0 12px;">Welcome to monitoraeo</h2>
      <p style="color:#475569; line-height:1.55; margin:0 0 14px;">
        Your <strong>{tier_label}</strong> purchase for {brand_line} is confirmed.
        Thanks for signing up — here's what happens next.
      </p>

      <h3 style="font-size:14px; text-transform:uppercase; letter-spacing:.06em;
                 color:#475569; margin:28px 0 8px;">Your audit is running</h3>
      <p style="color:#475569; line-height:1.55; margin:0;">
        We're querying the AI engines now. Your report will appear in your
        dashboard in 5–15 minutes and you'll get an email when it's ready.
      </p>

      <h3 style="font-size:14px; text-transform:uppercase; letter-spacing:.06em;
                 color:#475569; margin:28px 0 8px;">Your dashboard</h3>
      <p style="color:#475569; line-height:1.55; margin:0 0 16px;">
        Sign in any time to view this report, re-run the audit, edit the
        monitored brand and competitor list, and manage billing.
      </p>
      <p style="margin:0 0 8px;">
        {_button(dashboard_url, "Open my dashboard →")}
      </p>
      <p style="color:#94a3b8; font-size:13px; line-height:1.55; margin:6px 0 0;">
        You'll be asked for this email so we can send you a one-tap sign-in link.
        No password to remember.
      </p>
      {recurring_block}

      <h3 style="font-size:14px; text-transform:uppercase; letter-spacing:.06em;
                 color:#475569; margin:28px 0 8px;">Questions?</h3>
      <p style="color:#475569; line-height:1.55; margin:0;">
        Just reply to this email — it comes to a human.
      </p>
      <p style="color:#94a3b8; font-size:12px; line-height:1.5; margin:32px 0 0;">
        — monitoraeo
      </p>
    </div>
    """


def send_welcome(
    *,
    to_email: str,
    brand_name: str,
    tier: str,
    is_subscription: bool,
    dashboard_url: str,
) -> dict[str, Any]:
    """Post-purchase 'thanks for signing up' email. Fires from the Stripe
    webhook the moment payment confirms, so the user gets a confirmation
    even if they bail on the competitor-setup step."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set")
    resend.api_key = api_key

    from_addr = os.environ.get("REPORT_FROM_EMAIL", "monitoraeo <reports@monitoraeo.com>")
    subject = f"Welcome to monitoraeo — your {brand_name or 'brand'} audit has started"
    html = _build_welcome_html(
        brand_name=brand_name, tier=tier,
        is_subscription=is_subscription, dashboard_url=dashboard_url,
    )
    return resend.Emails.send({
        "from": from_addr,
        "to": [to_email],
        "subject": subject,
        "html": html,
    })
