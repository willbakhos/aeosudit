"""Capture a screenshot of the audited site via our self-hosted screenshotsys
service (Playwright-driven Chromium on Railway). Replaces the previous
screenshotapi.net SaaS — same call signature, different backend."""
from __future__ import annotations

import os
from pathlib import Path

import httpx

DEFAULT_TIMEOUT = 60.0


def capture(
    url: str,
    output_dir: Path,
    filename: str = "site_screenshot.png",
    full_page: bool = False,  # noqa: ARG001 — kept for callsite compat; new API doesn't support
    width: int = 1280,
    height: int = 800,
) -> Path | None:
    """Fetch a PNG screenshot from our screenshotsys instance and save it
    under output_dir. Returns the saved path, or None if env vars are missing
    or the call failed. Failures are non-fatal — the audit should still
    produce a report without a screenshot."""
    base = os.environ.get("SCREENSHOT_API_URL", "").strip().rstrip("/")
    token = os.environ.get("SCREENSHOT_API_TOKEN", "").strip()
    if not base or not token:
        return None

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            r = client.get(
                f"{base}/screenshot",
                params={"url": url, "width": str(width), "height": str(height)},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            path.write_bytes(r.content)
        return path
    except (httpx.HTTPError, OSError) as exc:
        err_log = output_dir / "errors.log"
        with err_log.open("a") as f:
            f.write(f"[screenshot] {url} -> {type(exc).__name__}: {exc}\n")
        return None
