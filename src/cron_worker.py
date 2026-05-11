"""Background cron worker for scheduled monitoring runs.

How it works
------------
Every CHECK_INTERVAL seconds the worker:
  1. Queries Postgres for brands whose `tier` is a monitoring tier AND
     whose `next_scheduled_run` is <= now (or null but has tier).
  2. Picks up to BATCH_SIZE brands per tick (the queue limiter — prevents
     the 10k-users-all-at-once problem).
  3. Spawns the existing `_run_audit_for_brand` background task per brand.
  4. Pre-emptively pushes `next_scheduled_run` forward to the 1st of the
     next month so a re-tick doesn't double-fire.

It runs in-process as a daemon thread started on FastAPI startup. For
larger scale you'd promote this to a separate process (Railway service)
and back the queue with Redis/Celery — but the model in this file is the
right shape for both.

Designed to be safe to enable in any environment: if DATABASE_URL isn't
set (CLI usage, local dev without Postgres), the loop just sleeps without
doing anything.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime
from uuid import UUID

# How often the worker wakes up to check for due brands.
CHECK_INTERVAL = int(os.environ.get("CRON_CHECK_INTERVAL_SEC", "300"))  # 5 min default

# How many brands to spawn per tick. With 10k users on the platform, batching
# at e.g. 20 every 5 min spreads load over ~40 hours — adjust based on actual
# Apify/OpenRouter throughput once we have real volume.
BATCH_SIZE = int(os.environ.get("CRON_BATCH_SIZE", "20"))

# Set to "0" to disable the cron worker entirely (useful during early debugging).
ENABLED = os.environ.get("CRON_ENABLED", "1") == "1"


_started = False
_lock = threading.Lock()


def start() -> None:
    """Start the worker thread (idempotent). Called from src/server.py startup."""
    global _started
    with _lock:
        if _started:
            return
        if not ENABLED:
            print("[cron] disabled via CRON_ENABLED=0")
            return
        if not os.environ.get("DATABASE_URL", "").strip():
            print("[cron] DATABASE_URL not set — worker won't run")
            return
        t = threading.Thread(target=_loop, daemon=True, name="monitoraeo-cron")
        t.start()
        _started = True
        print(f"[cron] worker started (check_interval={CHECK_INTERVAL}s, batch={BATCH_SIZE})")


def _loop() -> None:
    """Main worker loop. Wakes up every CHECK_INTERVAL seconds and processes
    a batch of due brands. All errors are caught — we never want the cron
    thread to die silently."""
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001
            print("[cron] tick failed:")
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL)


def _tick() -> None:
    """One pass of the worker. Find due brands, queue them, advance their
    next_scheduled_run forward."""
    # Imports inside the function so importing this module at startup doesn't
    # eagerly load the dashboard/db modules before they're ready.
    from sqlmodel import select
    from src.db import TrackedBrand, AuditRunRecord, get_session
    from src.dashboard import (
        _compute_next_scheduled_run,
        _run_audit_for_brand,
        _reset_monthly_counter_if_needed,
    )
    from src.server import _make_run_id

    now = datetime.utcnow()
    spawned = 0
    try:
        with get_session() as s:
            # Brands with a monitoring tier whose next_scheduled_run is due.
            due = list(
                s.exec(
                    select(TrackedBrand)
                    .where(TrackedBrand.tier != "")
                    .where(TrackedBrand.next_scheduled_run <= now)
                    .order_by(TrackedBrand.next_scheduled_run.asc())
                    .limit(BATCH_SIZE)
                )
            )
            if not due:
                return

            for brand in due:
                # Refresh monthly counter (the cron fires on the 1st, so this
                # also resets runs_this_month for the new month).
                _reset_monthly_counter_if_needed(brand)
                run_rec = AuditRunRecord(
                    brand_id=brand.id,
                    user_id=brand.user_id,
                    run_id=_make_run_id(brand.name),
                    status="running",
                )
                s.add(run_rec)
                # Advance the schedule BEFORE spawning so a slow run can't
                # cause a re-fire on the next tick.
                brand.next_scheduled_run = _compute_next_scheduled_run(now)
                s.add(brand)
                s.commit()
                s.refresh(run_rec)
                # Spawn the audit in a daemon thread.
                threading.Thread(
                    target=_run_audit_for_brand,
                    args=(str(brand.id), str(run_rec.id)),
                    daemon=True,
                ).start()
                spawned += 1
        if spawned:
            print(f"[cron] queued {spawned} brand(s) for audit at {now.isoformat()}Z")
    except Exception:  # noqa: BLE001
        print("[cron] _tick error:")
        traceback.print_exc()
