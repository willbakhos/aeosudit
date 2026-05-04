"""Email delivery via Resend. Sends a link to the hosted report + PDF attachment."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import resend

TIER_LABELS = {
    "spotlight": "Spotlight ($49)",
    "full": "Full Audit ($149)",
    "action_plan": "Audit + Action Plan ($349)",
}


def _build_html(brand_name: str, tier: str, report_url: str) -> str:
    tier_label = TIER_LABELS.get(tier, tier)
    return f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px; margin: 0 auto; color: #0f172a;">
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
) -> dict[str, Any]:
    """Send the audit report email. Returns the Resend response dict.
    Raises if RESEND_API_KEY is missing."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set")
    resend.api_key = api_key

    from_addr = os.environ.get("REPORT_FROM_EMAIL", "monitoraeo <reports@monitoraeo.com>")
    subject = f"Your {brand_name} AEO audit is ready"
    html = _build_html(brand_name, tier, report_url)

    params: dict[str, Any] = {
        "from": from_addr,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if pdf_path and pdf_path.exists():
        params["attachments"] = [
            {
                "filename": pdf_path.name,
                "content": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
            }
        ]

    return resend.Emails.send(params)
