"""Entry point: backfill historical CBU rates over a date range.

Walks day by day over a window (default: the last 2 years up to today),
fetching all currencies for each date from the historical endpoint and
upserting them. Designed to be run once to seed the warehouse, then handed
off to the daily job.

Robustness features required by the data source:
    * Resume-safe: dates already present in fact_rates are skipped, so the
      backfill can be re-run after an interruption without duplicating work.
    * Non-trading days: the historical endpoint returns an empty array for
      weekends/holidays; these are logged at INFO and skipped (no crash, no
      duplicated previous day).
    * Polite: a configurable delay (default 0.7s) is inserted between calls.
    * Fault-tolerant: each date is wrapped in try/except so a single failed
      request does not abort the whole ~730-call run.

Usage:
    python -m src.backfill                 # last 2 years
    python -m src.backfill --years 1
    python -m src.backfill --start 2024-01-01 --end 2024-06-30
    python -m src.backfill --delay 1.0

Exit codes:
    0  completed (even if some individual days failed -- failures are logged)
    1  fatal error (could not connect to DB, bad arguments)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

from src import cbu_client, database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")

DEFAULT_YEARS = 2
DEFAULT_DELAY_SECONDS = 0.7


def _daterange(start: date, end: date):
    """Yield each date from ``start`` to ``end`` inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the backfill."""
    parser = argparse.ArgumentParser(description="Backfill historical CBU rates.")
    parser.add_argument(
        "--years",
        type=int,
        default=DEFAULT_YEARS,
        help="Number of years to backfill from today (default: 2).",
    )
    parser.add_argument(
        "--start",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Explicit start date (YYYY-MM-DD); overrides --years.",
    )
    parser.add_argument(
        "--end",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Explicit end date (YYYY-MM-DD); defaults to today.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Polite delay in seconds between requests (default: 0.7).",
    )
    return parser.parse_args(argv)


def resolve_window(args: argparse.Namespace) -> tuple[date, date]:
    """Resolve the (start, end) backfill window from parsed arguments."""
    end = args.end or date.today()
    if args.start is not None:
        start = args.start
    else:
        # Approximate "N years" as N * 365 days; exactness is not required here.
        start = end - timedelta(days=args.years * 365)
    if start > end:
        raise ValueError(f"start ({start}) must not be after end ({end})")
    return start, end


def main(argv: list[str] | None = None) -> int:
    """Run the backfill over the resolved window. Returns a process exit code."""
    load_dotenv()
    args = parse_args(argv)

    try:
        start, end = resolve_window(args)
    except ValueError:
        logger.exception("Invalid date window")
        return 1

    logger.info("Backfilling %s .. %s (delay %.2fs)", start, end, args.delay)

    try:
        conn = database.get_connection()
    except Exception:  # noqa: BLE001
        logger.exception("Could not connect to the database")
        return 1

    try:
        database.bootstrap_schema(conn)
        done = database.existing_dates(conn)
        logger.info("%d dates already present; they will be skipped", len(done))

        session = requests.Session()
        total_days = 0
        loaded_days = 0
        skipped_existing = 0
        non_trading = 0
        errors = 0

        for day in _daterange(start, end):
            total_days += 1
            if day in done:
                skipped_existing += 1
                continue
            try:
                rates = cbu_client.fetch_historical(day, session=session)
            except Exception as exc:  # noqa: BLE001 -- per-day guard, keep going
                errors += 1
                logger.warning("Fetch failed for %s: %s", day, exc)
                time.sleep(args.delay)
                continue

            if not rates:
                non_trading += 1
                logger.info("No rates for %s (non-trading day); skipping", day)
            else:
                database.store_rates(conn, rates)
                loaded_days += 1
                logger.info("Loaded %d currencies for %s", len(rates), day)

            time.sleep(args.delay)

        logger.info(
            "Backfill done: %d days scanned, %d loaded, %d already present, "
            "%d non-trading, %d errors",
            total_days,
            loaded_days,
            skipped_existing,
            non_trading,
            errors,
        )

        if loaded_days or not done:
            exported = database.export_view_to_parquet(conn)
            logger.info("Exported %d rows to Parquet", exported)
    except Exception:  # noqa: BLE001
        logger.exception("Backfill failed")
        conn.rollback()
        return 1
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
