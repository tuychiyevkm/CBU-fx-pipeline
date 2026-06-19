"""Entry point: fetch the latest CBU rates, upsert, and re-export Parquet.

Intended to run once per day (GitHub Actions cron at 07:00 UTC = 12:00
Tashkent, after the CBU publishes the day's rates). Steps:

    1. Ensure the schema and view exist.
    2. Fetch the latest published rates for all currencies.
    3. Upsert dimension + fact (idempotent).
    4. Re-export the serving view to data/rates.parquet.

Exit codes:
    0  success
    1  failure (network, database, or no data)
"""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from src import cbu_client, database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_daily")


def main() -> int:
    """Run the daily fetch-upsert-export cycle. Returns a process exit code."""
    load_dotenv()

    try:
        logger.info("Fetching latest CBU rates")
        rates = cbu_client.fetch_daily()
    except Exception:  # noqa: BLE001 -- top-level guard; log and exit non-zero
        logger.exception("Failed to fetch daily rates from CBU")
        return 1

    if not rates:
        logger.error("CBU returned no rates; aborting without writing")
        return 1

    logger.info("Fetched %d currencies for %s", len(rates), rates[0].rate_date)

    try:
        conn = database.get_connection()
    except Exception:  # noqa: BLE001
        logger.exception("Could not connect to the database")
        return 1

    try:
        database.bootstrap_schema(conn)
        written = database.store_rates(conn, rates)
        logger.info("Stored %d fact rows", written)
        exported = database.export_view_to_parquet(conn)
        logger.info("Exported %d rows to Parquet", exported)
    except Exception:  # noqa: BLE001
        logger.exception("Daily load failed")
        conn.rollback()
        return 1
    finally:
        conn.close()

    logger.info("Daily fetch complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
