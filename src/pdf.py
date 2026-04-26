"""HTML → PDF using WeasyPrint. The audit report HTML is already self-contained
(inline CSS), so we just rasterise it. Embeds the screenshot if present."""
from __future__ import annotations

import base64
from pathlib import Path

from weasyprint import HTML


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
    html_path = run_dir / "report.html"
    if not html_path.exists():
        raise FileNotFoundError(f"No report.html in {run_dir}")
    html = html_path.read_text()
    html = _inline_screenshot(html, run_dir)
    pdf_path = run_dir / output_filename
    HTML(string=html, base_url=str(run_dir)).write_pdf(str(pdf_path))
    return pdf_path
