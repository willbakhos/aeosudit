"""Email delivery via Resend. Sends a link to the hosted report + PDF attachment."""
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


def _build_html(brand_name: str, tier: str, report_url: str, has_hero: bool) -> str:
    """Email body. If has_hero is True, embed a CID-attached hero image at the top."""
    tier_label = TIER_LABELS.get(tier, tier)
    hero_block = (
        '<img src="cid:report_hero" alt="" '
        'style="display:block; width:100%; max-width:560px; height:auto; '
        'border-radius:14px; margin:0 0 20px;">'
        if has_hero else ""
    )
    return f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px; margin: 0 auto; color: #0f172a;">
      {hero_block}
      <h2 style="font-size: 22px; margin: 0 0 12px;">Your {brand_name} AEO audit is ready</h2>
      <p style="color: #475569; line-height: 1.55;">
        Tier: <strong>{tier_label}</strong>
      </p>
      <p style="color: #475569; line-height: 1.55;">
        We tested how AI answer engines describe, cite and compare {brand_name}.
        The full report is attached as a PDF and is also available online:
      </p>
      <p style="margin: 24px 0;">
        <a href="{report_url}"
           style="display: inline-block; padding: 12px 22px; border-radius: 999px;
                  background: linear-gradient(135deg, #2563eb, #7c3aed);
                  color: white; text-decoration: none; font-weight: 700;">
          Open online report →
        </a>
      </p>
      <p style="color: #94a3b8; font-size: 12px; line-height: 1.5;">
        Questions? Just reply to this email.
      </p>
    </div>
    """


def send_report(
    *,
    to_email: str,
    brand_name: str,
    tier: str,
    report_url: str,
    pdf_path: Path | None = None,
    hero_image_path: Path | None = None,
) -> dict[str, Any]:
    """Send the audit report email. Returns the Resend response dict.
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
    html = _build_html(brand_name, tier, report_url, has_hero=has_hero)

    attachments: list[dict[str, Any]] = []
    if has_hero:
        attachments.append({
            "filename": "hero.png",
            "content": base64.b64encode(hero_image_path.read_bytes()).decode("ascii"),
            "content_id": "report_hero",
            "disposition": "inline",
        })
    if pdf_path and pdf_path.exists():
        attachments.append({
            "filename": pdf_path.name,
            "content": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
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


def _build_welcome_html(
    *, brand_name: str, tier: str, is_subscription: bool, dashboard_url: str
) -> str:
    tier_label = TIER_LABELS.get(tier, tier)
    brand_line = (
        f"<strong>{brand_name}</strong>" if brand_name else "your brand"
    )
    recurring_block = (
        f"""
      <h3 style="font-size:14px; text-transform:uppercase; letter-spacing:.06em;
                 color:#475569; margin:28px 0 8px;">Recurring billing</h3>
      <p style="color:#475569; line-height:1.55; margin:0;">
        You're now subscribed on the <strong>{tier_label}</strong> plan. We'll re-run
        the audit on the 1st of each month and email you the results. Cancel
        any time from your dashboard.
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
        We're querying the AI engines now. Your full report (hosted HTML + PDF + CSV)
        will land in this inbox in 5–15 minutes.
      </p>

      <h3 style="font-size:14px; text-transform:uppercase; letter-spacing:.06em;
                 color:#475569; margin:28px 0 8px;">Your dashboard</h3>
      <p style="color:#475569; line-height:1.55; margin:0 0 16px;">
        Sign in any time to re-open this report, re-run the audit, edit the
        monitored brand and competitor list, and manage billing.
      </p>
      <p style="margin:0 0 8px;">
        <a href="{dashboard_url}"
           style="display:inline-block; padding:12px 22px; border-radius:999px;
                  background:linear-gradient(135deg,#2563eb,#7c3aed);
                  color:white; text-decoration:none; font-weight:700;">
          Open my dashboard →
        </a>
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
