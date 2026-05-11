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
