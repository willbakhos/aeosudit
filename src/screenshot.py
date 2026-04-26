"""Capture a screenshot of the audited site via screenshotapi.net."""
from __future__ import annotations

import os
from pathlib import Path

import httpx

SCREENSHOTAPI_URL = "https://shot.screenshotapi.net/screenshot"
DEFAULT_TIMEOUT = 60.0


def capture(
    url: str,
    output_dir: Path,
    filename: str = "site_screenshot.png",
    full_page: bool = False,
    width: int = 1280,
    height: int = 800,
) -> Path | None:
    """Fetch a PNG screenshot and save it under output_dir.

    Returns the saved path, or None if no token is configured or the call failed.
    Failures are non-fatal — the audit should still produce a report.
    """
    token = os.environ.get("SCREENSHOTAPI_TOKEN", "").strip()
    if not token:
        return None

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    params = {
        "token": token,
        "url": url,
        "output": "image",
        "file_type": "png",
        "width": str(width),
        "height": str(height),
        "full_page": "true" if full_page else "false",
        "fresh": "true",
        "delay": "1500",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            r = client.get(SCREENSHOTAPI_URL, params=params)
            r.raise_for_status()
            path.write_bytes(r.content)
        return path
    except (httpx.HTTPError, OSError) as exc:
        # Best-effort: log and continue without screenshot
        err_log = output_dir / "errors.log"
        with err_log.open("a") as f:
            f.write(f"[screenshot] {url} -> {type(exc).__name__}: {exc}\n")
        return None
