"""PostgreSQL access layer for the CBU FX pipeline.

This module owns every interaction with the Supabase PostgreSQL warehouse:
connecting, bootstrapping the schema and view, upserting parsed rates, and
exporting the serving view to a Parquet file for Power BI.

Connection note (important for CI):
    Supabase free-tier direct connections are IPv6-only, while GitHub Actions
    runners are IPv4-only. The connection string MUST therefore use the
    *Session Pooler* host (IPv4) with the pooler username form
    ``postgres.<project-ref>``. The whole connection string is read from the
    ``DATABASE_URL`` environment variable; nothing is hardcoded.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras

from src.cbu_client import CurrencyRate

logger = logging.getLogger(__name__)

# Repo-root-relative paths so the module works regardless of CWD.
_REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL_PATH = _REPO_ROOT / "sql" / "schema.sql"
VIEW_SQL_PATH = _REPO_ROOT / "sql" / "view_rates_with_change.sql"
DEFAULT_PARQUET_PATH = _REPO_ROOT / "data" / "rates.parquet"


def get_connection(dsn: str | None = None) -> psycopg2.extensions.connection:
    """Open a PostgreSQL connection using the Session Pooler DSN.

    Args:
        dsn: optional connection string. If omitted, ``DATABASE_URL`` is read
            from the environment.

    Raises:
        RuntimeError: if no DSN is available.
    """
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Use the Supabase Session Pooler (IPv4) "
            "connection string, e.g. "
            "postgresql://postgres.<project-ref>:<password>@<region>.pooler.supabase.com:5432/postgres"
        )
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


def bootstrap_schema(conn: psycopg2.extensions.connection) -> None:
    """Create the star schema and serving view if they do not yet exist.

    Runs ``schema.sql`` then ``view_rates_with_change.sql``. Both scripts are
    idempotent, so this is safe to call on every run.
    """
    schema_sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    view_sql = VIEW_SQL_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        logger.info("Applying schema.sql")
        cur.execute(schema_sql)
        logger.info("Applying view_rates_with_change.sql")
        cur.execute(view_sql)
    conn.commit()
    logger.info("Schema and view are in place")


def upsert_currencies(conn: psycopg2.extensions.connection, rates: Sequence[CurrencyRate]) -> int:
    """Upsert the currency dimension from a batch of parsed rates.

    Deduplicates by ``currency_code`` within the batch, then inserts/updates
    descriptive attributes. Returns the number of distinct currencies written.
    """
    seen: dict[str, CurrencyRate] = {}
    for r in rates:
        seen[r.currency_code] = r  # last one wins; attributes are stable

    rows = [
        (
            r.currency_code,
            r.iso_numeric,
            r.name_en,
            r.name_ru,
            r.name_uz,
            r.name_uz_cyrillic,
            r.nominal,
        )
        for r in seen.values()
    ]
    if not rows:
        return 0

    sql = """
        INSERT INTO dim_currency
            (currency_code, iso_numeric, name_en, name_ru,
             name_uz, name_uz_cyrillic, nominal)
        VALUES %s
        ON CONFLICT (currency_code) DO UPDATE SET
            iso_numeric      = EXCLUDED.iso_numeric,
            name_en          = EXCLUDED.name_en,
            name_ru          = EXCLUDED.name_ru,
            name_uz          = EXCLUDED.name_uz,
            name_uz_cyrillic = EXCLUDED.name_uz_cyrillic,
            nominal          = EXCLUDED.nominal;
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows)
    conn.commit()
    logger.info("Upserted %d currencies", len(rows))
    return len(rows)


def upsert_rates(conn: psycopg2.extensions.connection, rates: Sequence[CurrencyRate]) -> int:
    """Upsert fact rows, idempotent on (rate_date, currency_code).

    Re-running for a date already present updates the values rather than
    creating duplicates. Returns the number of fact rows written.
    """
    rows = [
        (
            r.rate_date,
            r.currency_code,
            r.rate,
            r.rate_per_unit,
            r.diff,
        )
        for r in rates
    ]
    if not rows:
        logger.info("No rate rows to upsert")
        return 0

    sql = """
        INSERT INTO fact_rates
            (rate_date, currency_code, rate, rate_per_unit, diff)
        VALUES %s
        ON CONFLICT (rate_date, currency_code) DO UPDATE SET
            rate          = EXCLUDED.rate,
            rate_per_unit = EXCLUDED.rate_per_unit,
            diff          = EXCLUDED.diff;
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows)
    conn.commit()
    logger.info("Upserted %d fact rows", len(rows))
    return len(rows)


def store_rates(conn: psycopg2.extensions.connection, rates: Sequence[CurrencyRate]) -> int:
    """Upsert both dimension and fact for a batch of rates.

    Currencies are upserted first to satisfy the fact's foreign key. Returns
    the number of fact rows written.
    """
    upsert_currencies(conn, rates)
    return upsert_rates(conn, rates)


def existing_dates(
    conn: psycopg2.extensions.connection,
) -> set:
    """Return the set of distinct ``rate_date`` values already in fact_rates.

    Used by the backfill to skip dates that have already been loaded, making
    the backfill resume-safe.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT rate_date FROM fact_rates;")
        return {row[0] for row in cur.fetchall()}


def export_view_to_parquet(
    conn: psycopg2.extensions.connection,
    parquet_path: Path | str = DEFAULT_PARQUET_PATH,
) -> int:
    """Export the full serving view to a Parquet file for Power BI.

    Reads ``v_rates_with_change`` into a DataFrame and writes it to
    ``parquet_path`` (Snappy-compressed). Returns the row count exported.
    """
    parquet_path = Path(parquet_path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    query = """
        SELECT rate_date, currency_code, name_en, name_ru, nominal,
               rate, rate_per_unit, diff, prev_rate_per_unit, pct_change
        FROM v_rates_with_change
        ORDER BY currency_code, rate_date;
    """
    # Read via a raw cursor and build the DataFrame from rows + column names.
    # This avoids the SQLAlchemy-connectable requirement (and warning) of
    # pandas.read_sql_query, since psycopg2 connections are DBAPI2.
    with conn.cursor() as cur:
        cur.execute(query)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    frame = pd.DataFrame(rows, columns=columns)
    frame.to_parquet(parquet_path, index=False, engine="pyarrow", compression="snappy")
    logger.info("Exported %d rows to %s", len(frame), parquet_path)
    return len(frame)


def write_parquet_from_rows(
    rows: Iterable[dict],
    parquet_path: Path | str,
) -> int:
    """Write an arbitrary iterable of row dicts to Parquet.

    Helper used by the seed generator's synthetic path, which does not have a
    live database connection. Returns the row count written.
    """
    parquet_path = Path(parquet_path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(list(rows))
    frame.to_parquet(parquet_path, index=False, engine="pyarrow", compression="snappy")
    logger.info("Wrote %d rows to %s", len(frame), parquet_path)
    return len(frame)
