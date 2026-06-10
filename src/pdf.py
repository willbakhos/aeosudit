"""HTML → PDF. Two paths:
  - render_url_to_pdf_bytes(): goes via our screenshotsys service
    (Playwright/Chromium on Railway). Used for the email-gated
    /api/industry-pdf-request flow. Works on Railway.
  - render() / render_html_to_pdf_bytes(): in-process WeasyPrint. Used
    only by the paid-audit local report path. BROKEN on Railway today
    because the base image is missing libgobject — kept as fallback for
    local dev and any future Railway image that includes Pango.

WeasyPrint is imported LAZILY (inside the function) so just importing
src.pdf doesn't crash with `cannot load library 'libgobject-2.0-0'`
on Railway. Critical: render_url_to_pdf_bytes must work even when
WeasyPrint can't be imported at all."""
from __future__ import annotations

import base64
from pathlib import Path


def _inline_screenshot(html: str, run_dir: Path) -> str:
    """Inline the site_screenshot.png as a data URI so the PDF doesn't need
    file-system access at render time."""
    shot = run_dir / "site_screenshot.png"
    if not shot.exists():
        return html
    data = base64.b64encode(shot.read_bytes()).decode("ascii")
    data_uri = f"data:image/png;base64,{data}"
    return html.replace('src="site_screenshot.png"', f'src="{data_uri}"')


def render(run_dir: Path, output_filename: str = "report.pdf") -> Path:
    """Render report.html → report.pdf in the same directory."""
    from weasyprint import HTML  # lazy: see module docstring
    html_path = run_dir / "report.html"
    if not html_path.exists():
        raise FileNotFoundError(f"No report.html in {run_dir}")
    html = html_path.read_text()
    html = _inline_screenshot(html, run_dir)
    pdf_path = run_dir / output_filename
    HTML(string=html, base_url=str(run_dir)).write_pdf(str(pdf_path))
    return pdf_path


def render_html_to_pdf_bytes(html: str, base_url: str | None = None) -> bytes:
    """Render an HTML string directly to PDF bytes (no disk roundtrip).
    Uses WeasyPrint — only viable for paid-audit reports rendered locally.
    For industry-page renders on Railway use render_url_to_pdf_bytes()."""
    from weasyprint import HTML  # lazy: see module docstring
    result = HTML(string=html, base_url=base_url).write_pdf()
    if result is None:
        raise RuntimeError("WeasyPrint returned None from write_pdf")
    return bytes(result)


def render_url_to_pdf_bytes(
    url: str,
    *,
    format: str = "A4",
    landscape: bool = False,
    margin_px: int = 20,
    timeout_sec: float = 90.0,
) -> bytes:
    """Render a live URL to PDF bytes via our self-hosted screenshotsys
    service (Playwright/Chromium on Railway). Used by the email-gated
    /api/industry-pdf-request flow — works around the missing-libgobject
    issue that breaks WeasyPrint in Railway's Python image.

    The screenshotsys instance is the same one src/screenshot.py uses
    for site screenshots — reads SCREENSHOT_API_URL + SCREENSHOT_API_TOKEN
    from env. Raises RuntimeError when either is unset OR when the
    upstream returns non-2xx (the caller's outer try/except stamps the
    failure onto the IndustryPDFLead row)."""
    import os
    import httpx

    base = os.environ.get("SCREENSHOT_API_URL", "").strip().rstrip("/")
    token = os.environ.get("SCREENSHOT_API_TOKEN", "").strip()
    if not base or not token:
        raise RuntimeError(
            "SCREENSHOT_API_URL / SCREENSHOT_API_TOKEN are not set — "
            "PDF rendering needs the screenshotsys service"
        )

    params: dict[str, str] = {
        "url": url,
        "format": format,
        "margin": str(int(margin_px)),
    }
    if landscape:
        params["landscape"] = "1"

    with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
        resp = client.get(
            f"{base}/pdf",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code >= 400:
        # Trim the body so massive HTML error pages don't blow up the
        # IndustryPDFLead.error column (varchar — we slice to 300 chars
        # on the caller side, but be defensive).
        snippet = resp.text[:400].replace("\n", " ")
        raise RuntimeError(
            f"screenshotsys /pdf returned HTTP {resp.status_code}: {snippet}"
        )
    return resp.content
