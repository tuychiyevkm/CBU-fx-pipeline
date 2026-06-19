"""Tests for the database layer's upsert idempotency.

Two layers of testing:

1. A fast, dependency-free SQLite test that mirrors the ON CONFLICT upsert
   semantics. It proves the core invariant -- re-loading the same date does
   not create duplicate rows and does update changed values -- without needing
   a PostgreSQL server.

2. An optional integration test against a real PostgreSQL instance, run only
   when ``DATABASE_URL`` is set in the environment (skipped otherwise). This
   exercises the actual psycopg2 code paths in ``src.database``.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from src.cbu_client import CurrencyRate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rate(rate: str, diff: str = "1.00", day: str = "2026-06-18") -> CurrencyRate:
    """Build a USD CurrencyRate for a given rate/diff/date (test fixture)."""
    y, m, d = (int(x) for x in day.split("-"))
    return CurrencyRate(
        currency_code="USD",
        iso_numeric="840",
        name_en="US Dollar",
        name_ru="Доллар США",
        name_uz="AQSH dollari",
        name_uz_cyrillic="АҚШ доллари",
        nominal=1,
        rate=Decimal(rate),
        rate_per_unit=Decimal(rate),
        diff=Decimal(diff),
        rate_date=date(y, m, d),
    )


# ---------------------------------------------------------------------------
# SQLite mirror of the upsert -- fast, no server needed
# ---------------------------------------------------------------------------


def _sqlite_upsert(con: sqlite3.Connection, rates: list[CurrencyRate]) -> None:
    """Mirror of the ON CONFLICT upsert using SQLite UPSERT syntax.

    This intentionally reproduces the same conflict target and update behaviour
    as ``src.database.upsert_rates`` so the idempotency invariant can be tested
    without PostgreSQL.
    """
    con.executemany(
        """
        INSERT INTO fact_rates (rate_date, currency_code, rate, rate_per_unit, diff)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (rate_date, currency_code) DO UPDATE SET
            rate          = excluded.rate,
            rate_per_unit = excluded.rate_per_unit,
            diff          = excluded.diff
        """,
        [
            (
                r.rate_date.isoformat(),
                r.currency_code,
                str(r.rate),
                str(r.rate_per_unit),
                str(r.diff),
            )
            for r in rates
        ],
    )
    con.commit()


@pytest.fixture()
def sqlite_con() -> sqlite3.Connection:
    """An in-memory SQLite DB with a fact_rates table matching the schema."""
    con = sqlite3.connect(":memory:")
    con.execute(
        """
        CREATE TABLE fact_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rate_date TEXT NOT NULL,
            currency_code TEXT NOT NULL,
            rate TEXT NOT NULL,
            rate_per_unit TEXT NOT NULL,
            diff TEXT NOT NULL,
            UNIQUE (rate_date, currency_code)
        )
        """
    )
    con.commit()
    return con


def test_upsert_is_idempotent_no_duplicates(sqlite_con: sqlite3.Connection) -> None:
    """Loading the same date twice yields exactly one row (no duplicates)."""
    rate = _make_rate("12052.05")
    _sqlite_upsert(sqlite_con, [rate])
    _sqlite_upsert(sqlite_con, [rate])  # same date again
    count = sqlite_con.execute("SELECT COUNT(*) FROM fact_rates").fetchone()[0]
    assert count == 1


def test_upsert_updates_changed_values(sqlite_con: sqlite3.Connection) -> None:
    """A re-load with a corrected rate updates the row in place."""
    _sqlite_upsert(sqlite_con, [_make_rate("12000.00")])
    _sqlite_upsert(sqlite_con, [_make_rate("12052.05")])  # corrected value
    rows = sqlite_con.execute("SELECT rate FROM fact_rates WHERE rate_date='2026-06-18'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "12052.05"


def test_upsert_distinct_dates_accumulate(sqlite_con: sqlite3.Connection) -> None:
    """Different dates for the same currency create separate rows."""
    _sqlite_upsert(sqlite_con, [_make_rate("12000.00", day="2026-06-17")])
    _sqlite_upsert(sqlite_con, [_make_rate("12052.05", day="2026-06-18")])
    count = sqlite_con.execute("SELECT COUNT(*) FROM fact_rates").fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# Optional integration test against real PostgreSQL
# ---------------------------------------------------------------------------

_INTEGRATION = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; skipping PostgreSQL integration test",
)


@_INTEGRATION
def test_postgres_upsert_idempotent() -> None:
    """Integration: real psycopg2 upsert against a live PostgreSQL database.

    Runs only when DATABASE_URL is configured. Uses a transaction that is
    rolled back so the test leaves no residue.
    """
    from src import database

    conn = database.get_connection()
    try:
        database.bootstrap_schema(conn)
        rate = _make_rate("99999.00", day="1900-01-01")  # sentinel test date
        database.store_rates(conn, [rate])
        database.store_rates(conn, [rate])  # second load must not duplicate
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM fact_rates WHERE rate_date = %s AND currency_code = %s",
                (rate.rate_date, rate.currency_code),
            )
            assert cur.fetchone()[0] == 1
            # Clean up the sentinel row.
            cur.execute(
                "DELETE FROM fact_rates WHERE rate_date = %s AND currency_code = %s",
                (rate.rate_date, rate.currency_code),
            )
        conn.commit()
    finally:
        conn.close()
