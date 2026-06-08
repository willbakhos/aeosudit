"""Single factory for the Google AI search engine across the whole app.

Every code path that historically instantiated `ApifyEngine(label=..., ...)`
should call `build_default_engine(...)` instead. The factory reads
SEARCH_ENGINE (preferred) or INDUSTRY_SEARCH_ENGINE (legacy alias) from
the environment and picks the implementation:

    SEARCH_ENGINE=searchapi   → SearchApiEngine (Google AI Mode)  [default]
    SEARCH_ENGINE=apify       → ApifyEngine     (Google AI Overviews scraper)

Apify is kept in the repo as a one-flag rollback only — paying customers
and the free preview both run on SearchApi by default now.

The canonical engine label is "Google AI Mode" — this is what every new
audit row records to `top_engine`, what tier filters select on, and what
JSON-LD reports. Historical Apify rows in the DB keep their old
"Google AI Overviews" labels; nothing rewrites history.
"""
from __future__ import annotations

import os
from typing import Any

# Canonical user-visible engine name. The same string appears in tier
# filters (dashboard.py), Schema.org HowTo tool lists, and customer
# report copy — all of which must use this constant so we never drift.
DEFAULT_ENGINE_LABEL = "Google AI Mode"

# Legacy label used by Apify-scraped AIO results. Kept so historical
# DB rows with this label still match tier filters when needed.
LEGACY_AIO_LABEL = "Google AI Overviews"


def _selected_backend() -> str:
    """Read the engine selector env var, with INDUSTRY_SEARCH_ENGINE as a
    backwards-compat alias for SEARCH_ENGINE. Defaults to 'searchapi'."""
    raw = (
        os.environ.get("SEARCH_ENGINE")
        or os.environ.get("INDUSTRY_SEARCH_ENGINE")
        or "searchapi"
    )
    return raw.strip().lower()


def build_default_engine(
    *,
    country_code: str = "us",
    language_code: str = "en",
    label: str | None = None,
) -> Any:
    """Return the configured Google AI search engine instance.

    `label` overrides the engine's canonical label — used by config-driven
    paths that want a custom display name. When omitted, the engine's own
    canonical label (DEFAULT_ENGINE_LABEL for SearchApi, LEGACY_AIO_LABEL
    for Apify) is used so reports + filters stay in sync.

    Raises RuntimeError when the engine's auth env var is missing — caller
    is expected to handle this (industry_audit uses _short_backoff_schedule
    to avoid hot-looping)."""
    backend = _selected_backend()
    if backend == "apify":
        from src.engines.apify import ApifyEngine
        return ApifyEngine(
            label=label or LEGACY_AIO_LABEL,
            country_code=country_code,
            language_code=language_code,
        )
    # Default + any unrecognised value → SearchApi
    from src.engines.searchapi import SearchApiEngine
    return SearchApiEngine(
        label=label or DEFAULT_ENGINE_LABEL,
        country_code=country_code,
        language_code=language_code,
    )


def engine_display_name() -> str:
    """Human-facing engine name for template/JSON-LD/methodology copy.
    Mirrors build_default_engine — they must stay in sync."""
    backend = _selected_backend()
    return LEGACY_AIO_LABEL if backend == "apify" else DEFAULT_ENGINE_LABEL
