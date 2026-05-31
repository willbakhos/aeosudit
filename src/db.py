"""monitor-dashboard persistence: SQLModel engine + tables for tracked brands
and audit-run history.

Targets Supabase Postgres (set DATABASE_URL). The dashboard server scopes every
query by the Supabase user_id from the session cookie. Supabase Auth owns the
auth.users table; we keep our own tables in `public` and reference user_id by
UUID. No FK to auth.users so a missing supabase project doesn't fail bootstrap.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from uuid import UUID, uuid4

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Session, SQLModel, create_engine


DATABASE_URL = os.environ.get("DATABASE_URL", "")

_engine = None


def engine():
    """Lazy engine — lets the rest of the app import this module even when
    DATABASE_URL isn't configured (e.g. for the CLI / one-shot audit flow)."""
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set — the monitor-dashboard needs Supabase Postgres."
            )
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return _engine


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(engine()) as s:
        yield s


def init_db() -> None:
    """Create tables if they don't exist + apply additive column migrations.
    Safe to call on every server start (idempotent via IF NOT EXISTS)."""
    from sqlalchemy import text
    eng = engine()
    SQLModel.metadata.create_all(eng)

    # Additive migrations — Postgres ALTER TABLE IF NOT EXISTS keeps this idempotent.
    # Whenever you add a column to a SQLModel above, add the matching ALTER here.
    migrations = [
        "ALTER TABLE monitor_tracked_brand ADD COLUMN IF NOT EXISTS tier VARCHAR DEFAULT ''",
        "ALTER TABLE monitor_tracked_brand ADD COLUMN IF NOT EXISTS last_run_at TIMESTAMP",
        "ALTER TABLE monitor_tracked_brand ADD COLUMN IF NOT EXISTS next_scheduled_run TIMESTAMP",
        "ALTER TABLE monitor_tracked_brand ADD COLUMN IF NOT EXISTS runs_this_month INTEGER DEFAULT 0",
        "ALTER TABLE monitor_tracked_brand ADD COLUMN IF NOT EXISTS runs_month_anchor TIMESTAMP",
        "ALTER TABLE monitor_tracked_brand ADD COLUMN IF NOT EXISTS monitored_queries JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE monitor_audit_run ADD COLUMN IF NOT EXISTS trigger_type VARCHAR DEFAULT 'manual'",
        "ALTER TABLE monitor_tracked_brand ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR",
        # IndustryReport / IndustryBrand additive columns. Both tables are
        # created by create_all above; the ALTERs below let us add columns to
        # an already-deployed schema without an explicit migration tool.
        "ALTER TABLE monitor_industry_report ADD COLUMN IF NOT EXISTS parent_category VARCHAR DEFAULT ''",
        "ALTER TABLE monitor_industry_report ADD COLUMN IF NOT EXISTS methodology_version INTEGER DEFAULT 1",
        "ALTER TABLE monitor_industry_report ADD COLUMN IF NOT EXISTS refresh_interval_days INTEGER DEFAULT 30",
        "ALTER TABLE monitor_industry_brand ADD COLUMN IF NOT EXISTS visibility_pct DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE monitor_industry_brand ADD COLUMN IF NOT EXISTS citation_pct DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE monitor_industry_brand ADD COLUMN IF NOT EXISTS top_engine VARCHAR DEFAULT ''",
        "ALTER TABLE monitor_industry_brand ADD COLUMN IF NOT EXISTS top_cited_sources JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE monitor_industry_brand ADD COLUMN IF NOT EXISTS last_audit_error VARCHAR",
        "CREATE INDEX IF NOT EXISTS idx_industry_brand_slug_rank ON monitor_industry_brand (industry_slug, rank_in_industry)",
        # One-off repair: the admin form's old `lstrip('https://')` ate the
        # first character of any brand_domain whose first letter happened to
        # match h/t/p/s (the lstrip char set). Fix surgically by exact
        # (brand_name, broken_domain) match — never touches a row whose
        # domain looks intentional. Idempotent (no-op once domains are
        # correct). Add new pairs here if more truncations surface.
        "UPDATE monitor_industry_brand SET brand_domain = 'hubspot.com'    WHERE brand_name = 'HubSpot'     AND brand_domain = 'ubspot.com'",
        "UPDATE monitor_industry_brand SET brand_domain = 'salesforce.com' WHERE brand_name = 'Salesforce'  AND brand_domain = 'alesforce.com'",
        "UPDATE monitor_industry_brand SET brand_domain = 'pipedrive.com'  WHERE brand_name = 'Pipedrive'   AND brand_domain = 'ipedrive.com'",
        "UPDATE monitor_industry_brand SET brand_domain = 'sugarcrm.com'   WHERE brand_name = 'SugarCRM'    AND brand_domain = 'ugarcrm.com'",
        # DefinitionalPage additive columns — create_all handles the table,
        # ALTERs cover columns added after first deploy.
        "ALTER TABLE monitor_definitional_page ADD COLUMN IF NOT EXISTS parent_section VARCHAR DEFAULT 'Concepts'",
        "ALTER TABLE monitor_definitional_page ADD COLUMN IF NOT EXISTS target_kw VARCHAR DEFAULT ''",
        "ALTER TABLE monitor_definitional_page ADD COLUMN IF NOT EXISTS short_definition VARCHAR DEFAULT ''",
        "ALTER TABLE monitor_definitional_page ADD COLUMN IF NOT EXISTS meta_description VARCHAR DEFAULT ''",
        "ALTER TABLE monitor_definitional_page ADD COLUMN IF NOT EXISTS related_slugs JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE monitor_definitional_page ADD COLUMN IF NOT EXISTS alternate_names JSONB DEFAULT '[]'::jsonb",
    ]
    with eng.begin() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
            except Exception as exc:  # noqa: BLE001
                print(f"[init_db] migration skipped: {sql} -> {type(exc).__name__}: {exc}")


class TrackedBrand(SQLModel, table=True):
    """A brand the user is monitoring. Holds everything the audit pipeline
    needs — name, domain, aliases, competitors, ground-truth facts, locale —
    so a run can be built from one row."""
    __tablename__ = "monitor_tracked_brand"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(index=True)  # Supabase auth.users.id
    name: str
    domain: str
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    competitors: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB)
    )
    ground_truth: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    engines: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    locale_country: str = "US"
    locale_language: str = "en"

    # Subscription tier — drives which engines run on the scheduled cron.
    # "two_engine_monthly" -> Google AI + ChatGPT
    # "full_monthly"       -> all 5 engines
    # ""                   -> no monitoring subscription, brand is dashboard-only
    tier: str = ""

    # Scheduling state — updated by the cron worker on each run.
    # last_run_at: when the most recent successful run completed.
    # next_scheduled_run: when the next automatic run will fire (1st of month).
    # runs_this_month: count of runs in the current calendar month (auto + manual).
    # Subscribers are capped at 2 runs per month (cron + 1 manual re-run).
    last_run_at: datetime | None = None
    next_scheduled_run: datetime | None = None
    runs_this_month: int = 0
    runs_month_anchor: datetime | None = None

    # Stripe customer ID for the brand owner. Captured on subscription
    # checkout; lazily backfilled by email lookup when the owner first buys
    # an add-on run. Required for off-session PaymentIntent charging.
    stripe_customer_id: str | None = None

    # The list of buyer-question strings to run each monitoring cycle. Empty
    # means "use the generic 8 templated brand questions" (lazy fallback) —
    # which is how free / unconfigured brands work. Subscribers edit this on
    # /dashboard/brands/{id}/queries up to their tier's monitored_query_limit.
    monitored_queries: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB)
    )

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Purchase(SQLModel, table=True):
    """Every successful Stripe charge — one-off audits AND each subscription
    billing cycle. Keyed by stripe id (idempotent) and matched to a Supabase
    user by lowercase email. Drives the admin "report purchases" + revenue
    columns. Created from the /webhooks/stripe handler."""
    __tablename__ = "monitor_purchase"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(index=True)              # lowercased
    tier: str                                   # two_engine | full_audit | two_engine_monthly | full_monthly
    amount_usd: float = 0.0                     # at-the-time tier price (USD)
    brand_name: str = ""
    domain: str = ""
    stripe_event_id: str = Field(default="", index=True)   # webhook event id
    stripe_session_id: str = Field(default="", index=True) # checkout session id (one-offs + first sub charge)
    stripe_invoice_id: str = Field(default="", index=True) # invoice id (sub renewals)
    kind: str = "one_off"                       # one_off | subscription_initial | subscription_renewal
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeamInvite(SQLModel, table=True):
    """Pending invite to join an owner's workspace. Created from the Team
    settings page; consumed when the invitee signs in via Supabase magic
    link with a matching email and the auth flow checks for invites."""
    __tablename__ = "monitor_team_invite"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    owner_user_id: UUID = Field(index=True)  # who sent the invite
    email: str = Field(index=True)            # who they invited (lowercased)
    token: str = Field(default_factory=lambda: uuid4().hex, index=True)
    status: str = "pending"                   # pending | accepted | revoked
    created_at: datetime = Field(default_factory=datetime.utcnow)
    accepted_at: datetime | None = None


class TeaserShortlink(SQLModel, table=True):
    """Short-link backing for /api/teaser/email-generated URLs. We hand the
    cold-email recipient a `/preview?d=<integer-id>` link instead of stuffing
    the full domain into the query string — shorter to scan and harder to
    enumerate. The serial id is the public token; the domain + brand +
    category live on the row and are resolved by /preview at click time."""
    __tablename__ = "monitor_teaser_shortlink"

    id: int | None = Field(default=None, primary_key=True)
    domain: str = Field(index=True)
    brand: str
    category: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AuditRunRecord(SQLModel, table=True):
    """One audit execution against a tracked brand. Headline metrics are
    denormalised onto the row so the trend chart can render from a single
    SELECT — full per-query results stay on disk under OUTPUT_ROOT/{run_id}."""
    __tablename__ = "monitor_audit_run"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    brand_id: UUID = Field(index=True, foreign_key="monitor_tracked_brand.id")
    user_id: UUID = Field(index=True)
    run_id: str = Field(index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    status: str = "running"  # running | complete | failed
    error: str | None = None
    queries_total: int = 0
    visibility_rate: float | None = None
    citation_rate: float | None = None
    sentiment_avg: float | None = None
    accuracy_avg: float | None = None
    hallucination_rate: float | None = None
    share_of_voice: dict[str, int] = Field(
        default_factory=dict, sa_column=Column(JSONB)
    )
    # How this run was triggered. "scheduled" = monthly cron auto-fire;
    # "manual" = any user-initiated run (dashboard re-run, master override,
    # post-purchase fulfilment). Used by the trend chart to colour-code
    # manual interventions distinctly from the scheduled baseline.
    trigger_type: str = "manual"


class IndustryReport(SQLModel, table=True):
    """A public industry visibility ranking page at /ai-visibility/{slug}.
    Drives the programmatic-SEO moat: every industry we cover becomes one
    indexable page ranking the top N brands in that category by their AI
    answer-engine visibility. Refreshed monthly by the cron worker."""
    __tablename__ = "monitor_industry_report"

    slug: str = Field(primary_key=True)          # "crm-software"
    name: str                                     # "CRM software"
    parent_category: str = ""                     # "SaaS" | "Fintech" | "Productivity" | "Marketing" | "Creative"
    description: str = ""                         # 1-sentence category def for the page lede
    methodology_version: int = 1                  # bump when scoring math changes (invalidates trends)
    refresh_interval_days: int = 30               # how often each brand re-audits
    last_full_refresh: datetime | None = None     # most recent successful pass over all brands
    next_scheduled_refresh: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DefinitionalPage(SQLModel, table=True):
    """A glossary / definitional content page rendered at /glossary/{slug}.
    Built so editorial content can be published programmatically (via the
    /api/definitional-pages endpoints) without code changes — same shape
    as the IndustryReport pattern but for content rather than data.

    The high-traffic glossary pages (what-is-aeo, what-is-ai-mode etc.) are
    custom Jinja templates at root URLs for richer visual treatment. Pages
    backed by this model live at /glossary/{slug} and use one generic
    template — uniform shape, fast to populate, contractor-friendly."""
    __tablename__ = "monitor_definitional_page"

    slug: str = Field(primary_key=True)          # "share-of-voice-in-ai-answers"
    name: str                                     # "Share of voice in AI answers"
    parent_section: str = "Concepts"              # Concepts | Engines | Metrics | Tactics | Google AI surfaces
    target_kw: str = ""                           # primary KW used in title + meta
    short_definition: str = ""                    # ~30 words for glossary index card
    meta_description: str = ""                    # ~155 chars — falls back to short_definition
    lede: str = ""                                # ~50 words, displayed under H1
    # Sections are [{heading: str, body_html: str}, ...]. body_html is trusted
    # HTML (admin-authed) — supports any markup including <a>, lists, tables,
    # <pre>, <strong>, <em>. The template wraps each section in H2 + the body.
    sections: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB))
    # FAQs are [{q: str, a: str}, ...]. Rendered as <details> and as FAQPage schema.
    faqs: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB))
    # Slugs of other glossary pages to cross-link from the "Related" footer.
    # Can reference either custom pages (root URLs) or DB pages (/glossary/{slug}).
    related_slugs: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    # Aliases for DefinedTerm.alternateName (e.g. "Generative AI Optimisation")
    alternate_names: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    published_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class IndustryBrand(SQLModel, table=True):
    """One brand inside an IndustryReport. The (industry_slug, brand_domain)
    pair is unique. Each row carries the cached audit result so the public
    page can render with a single SELECT — no live audit calls on render."""
    __tablename__ = "monitor_industry_brand"

    id: int | None = Field(default=None, primary_key=True)
    industry_slug: str = Field(index=True)         # foreign key to IndustryReport.slug
    brand_name: str
    brand_domain: str = Field(index=True)
    # Composite 0-100 score: weighted average of visibility (% of answers
    # naming brand) and citation rate (% citing brand's domain). Higher = better.
    visibility_score: float = 0.0
    # Raw visibility (% of answers naming the brand at all). 0-100.
    visibility_pct: float = 0.0
    # Raw citation rate (% of answers citing the brand's domain). 0-100.
    citation_pct: float = 0.0
    # Per-engine breakdown: {"google_ai": {"visibility": 50, "citations": 25}, ...}
    visibility_per_engine: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB)
    )
    # Top 5 domains the AI cited when answering category questions. Used to
    # show "AI's trusted sources in this category" on the page.
    top_cited_sources: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB)
    )
    # The single strongest engine for this brand. Surfaced as the "top engine"
    # column in the ranking table.
    top_engine: str = ""
    rank_in_industry: int = 0                      # cached for sort, recomputed on full refresh
    last_audited: datetime | None = None
    last_audit_error: str | None = None            # last failure reason, for triage
    created_at: datetime = Field(default_factory=datetime.utcnow)
