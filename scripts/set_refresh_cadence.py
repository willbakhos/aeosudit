"""One-shot: move industry rankings from a 30-day to a 90-day refresh cadence.

Deliberately a script, NOT an entry in db.py's permanent migration list. A
permanent `UPDATE ... SET refresh_interval_days = 90 WHERE ... = 30` would
re-fire on every deploy and silently clobber any industry an operator later
puts back on a faster cadence on purpose. This runs once, by hand.

Rescheduling: next_scheduled_refresh is recomputed as
last_full_refresh + <new interval> so the saving starts immediately rather
than after one more cycle at the old cadence. Rows whose recomputed date is
already in the past are left overdue on purpose, so the cron picks them up
on its normal schedule.

Usage:
    python -m scripts.set_refresh_cadence --dry-run
    python -m scripts.set_refresh_cadence --apply
    python -m scripts.set_refresh_cadence --apply --from-days 30 --to-days 90
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

from sqlmodel import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import IndustryReport, get_session


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-days", type=int, default=30,
                    help="Only touch rows currently on this interval (default 30).")
    ap.add_argument("--to-days", type=int, default=90,
                    help="New interval to set (default 90).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write. Without this the script only reports.")
    ap.add_argument("--dry-run", action="store_true", help="Explicit no-op mode.")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        sys.exit("--apply and --dry-run are mutually exclusive")

    now = datetime.utcnow()
    with get_session() as s:
        rows = list(s.exec(
            select(IndustryReport).where(
                IndustryReport.refresh_interval_days == args.from_days
            )
        ))
        total_all = len(list(s.exec(select(IndustryReport))))

        print(f"industries total                 : {total_all}")
        print(f"on {args.from_days}-day cadence (will change) : {len(rows)}")
        print(f"on another cadence (left alone)  : {total_all - len(rows)}")

        if not rows:
            print("nothing to do")
            return

        becomes_overdue = 0
        no_refresh_yet = 0
        for r in rows:
            if r.last_full_refresh is None:
                no_refresh_yet += 1
            elif r.last_full_refresh + timedelta(days=args.to_days) <= now:
                becomes_overdue += 1
        print(f"\nafter reschedule to last_full_refresh + {args.to_days}d:")
        print(f"  immediately due (overdue)      : {becomes_overdue}")
        print(f"  scheduled in the future        : {len(rows) - becomes_overdue - no_refresh_yet}")
        print(f"  never refreshed (left as-is)   : {no_refresh_yet}")

        if not args.apply:
            print("\n(dry run — pass --apply to write)")
            return

        # One bulk UPDATE rather than 2400+ ORM writes. Row-by-row through
        # the Supabase pooler took minutes and timed out before committing;
        # this is a single round trip.
        from sqlalchemy import text as _text
        res = s.exec(_text(
            "UPDATE monitor_industry_report "
            "SET refresh_interval_days = :to_days, "
            "    next_scheduled_refresh = CASE "
            "        WHEN last_full_refresh IS NOT NULL "
            "        THEN last_full_refresh + (:to_days || ' days')::interval "
            "        ELSE next_scheduled_refresh END "
            "WHERE refresh_interval_days = :from_days"
        ).bindparams(to_days=args.to_days, from_days=args.from_days))
        s.commit()
        changed = getattr(res, "rowcount", None)
        print(f"\napplied: {changed if changed is not None else len(rows)} "
              f"industries now on a {args.to_days}-day cadence")

    # Public pages cache the interval in their copy; drop the cache so the
    # "Refreshed every N days" byline updates on the next request.
    try:
        from src.server import invalidate_ai_visibility_cache
        invalidate_ai_visibility_cache()
        print("ai-visibility cache invalidated")
    except Exception as exc:  # noqa: BLE001
        print(f"cache invalidation skipped: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
