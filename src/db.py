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

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


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
