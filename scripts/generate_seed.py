"""Generate a 30-day seed dataset for the CBU FX pipeline.

Real purpose (default mode): fetch the last 30 days from the live CBU
historical endpoint, compute the daily pct_change exactly as the SQL view
would, and write a self-contained serving file plus a SQL seed script:

    data/seed/rates_seed.parquet   -- the view-shaped serving rows
    data/seed/seed.sql             -- INSERTs for dim_currency + fact_rates
    data/rates.parquet             -- copy used as the live Power BI source

Fallback (``--demo``): if the live API is unreachable, generate a CLEARLY
LABELED synthetic 30-day seed by random-walking from a single real sample
object. In that case an extra marker file is written:

    data/seed/IS_SYNTHETIC.txt

The synthetic seed exists only so the repository is demonstrable offline.
Before publishing, delete data/seed/IS_SYNTHETIC.txt and re-run this script
WITHOUT --demo (or with --live) so the seed contains real CBU data.

Usage:
    python scripts/generate_seed.py            # try live; do NOT auto-fake
    python scripts/generate_seed.py --demo      # force synthetic seed
    python scripts/generate_seed.py --live      # live only; fail if unreachable
    python scripts/generate_seed.py --days 30
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import requests

# Allow running as a plain script (python scripts/generate_seed.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import cbu_client  # noqa: E402
from src.cbu_client import CurrencyRate  # noqa: E402
from src.database import write_parquet_from_rows  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("generate_seed")

_REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = _REPO_ROOT / "data" / "seed"
SEED_PARQUET = SEED_DIR / "rates_seed.parquet"
SEED_SQL = SEED_DIR / "seed.sql"
SYNTHETIC_MARKER = SEED_DIR / "IS_SYNTHETIC.txt"
LIVE_PARQUET = _REPO_ROOT / "data" / "rates.parquet"

# One real sample object (USD), used as the anchor for the synthetic walk.
REAL_SAMPLE = {
    "id": 68,
    "Code": "840",
    "Ccy": "USD",
    "CcyNm_RU": "Доллар США",
    "CcyNm_UZ": "AQSH dollari",
    "CcyNm_UZC": "АҚШ доллари",
    "CcyNm_EN": "US Dollar",
    "Nominal": "1",
    "Rate": "12021.22",
    "Diff": "-54.96",
    "Date": "22.05.2026",
}

# A few extra anchors so the synthetic seed has variety, including a
# Nominal=10 currency (IDR) to exercise the standardization path.
SYNTHETIC_ANCHORS = [
    REAL_SAMPLE,
    {
        "id": 20,
        "Code": "978",
        "Ccy": "EUR",
        "CcyNm_RU": "Евро",
        "CcyNm_UZ": "EVRO",
        "CcyNm_UZC": "EВРО",
        "CcyNm_EN": "Euro",
        "Nominal": "1",
        "Rate": "13985.20",
        "Diff": "49.94",
        "Date": "22.05.2026",
    },
    {
        "id": 56,
        "Code": "643",
        "Ccy": "RUB",
        "CcyNm_RU": "Российский рубль",
        "CcyNm_UZ": "Rossiya rubli",
        "CcyNm_UZC": "Россия рубли",
        "CcyNm_EN": "Russian Ruble",
        "Nominal": "1",
        "Rate": "165.30",
        "Diff": "-0.82",
        "Date": "22.05.2026",
    },
    {
        "id": 14,
        "Code": "156",
        "Ccy": "CNY",
        "CcyNm_RU": "Юань",
        "CcyNm_UZ": "Xitoy yuani",
        "CcyNm_UZC": "Хитой юани",
        "CcyNm_EN": "Yuan Renminbi",
        "Nominal": "1",
        "Rate": "1783.45",
        "Diff": "5.59",
        "Date": "22.05.2026",
    },
    {
        "id": 25,
        "Code": "360",
        "Ccy": "IDR",
        "CcyNm_RU": "Рупия",
        "CcyNm_UZ": "Indoneziya rupiyasi",
        "CcyNm_UZC": "Индонезия рупияси",
        "CcyNm_EN": "Rupiah",
        "Nominal": "10",
        "Rate": "6.80",
        "Diff": "0.01",
        "Date": "22.05.2026",
    },
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Generate a 30-day CBU seed.")
    parser.add_argument(
        "--days", type=int, default=30, help="Number of days to seed (default: 30)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Force a synthetic seed (offline demo).")
    mode.add_argument(
        "--live", action="store_true", help="Live only; fail if the API is unreachable."
    )
    return parser.parse_args(argv)


def fetch_live_seed(days: int) -> dict[date, list[CurrencyRate]]:
    """Fetch the last ``days`` calendar days from the live historical endpoint.

    Returns a mapping of date -> parsed rates. Non-trading days simply do not
    appear in the map. Raises on a total inability to reach the API.
    """
    session = requests.Session()
    end = date.today()
    by_date: dict[date, list[CurrencyRate]] = {}
    reachable = False
    for offset in range(days):
        day = end - timedelta(days=offset)
        rates = cbu_client.fetch_historical(day, session=session)
        reachable = True  # got a response (even if empty)
        if rates:
            by_date[day] = rates
            logger.info("Live: %d currencies for %s", len(rates), day)
        else:
            logger.info("Live: no rates for %s (non-trading day)", day)
    if not reachable:
        raise requests.RequestException("CBU API unreachable")
    return by_date


def build_synthetic_seed(days: int) -> dict[date, list[CurrencyRate]]:
    """Build a synthetic 30-day seed via a small random walk from anchors.

    Each anchor currency drifts by a small daily percentage. Weekends are
    skipped to imitate non-trading days. Values are SYNTHETIC and must be
    replaced with real data before publishing.
    """
    rng = random.Random(42)  # deterministic for reproducible demos
    end = date.today()
    # Seed the walk with each anchor's standardized current value.
    current = {a["Ccy"]: cbu_client.parse_rate(a) for a in SYNTHETIC_ANCHORS}
    by_date: dict[date, list[CurrencyRate]] = {}

    for offset in range(days - 1, -1, -1):  # oldest -> newest
        day = end - timedelta(days=offset)
        if day.weekday() >= 5:  # Sat/Sun -> non-trading
            continue
        rows: list[CurrencyRate] = []
        for ccy, base in current.items():
            drift = Decimal(str(round(rng.uniform(-0.008, 0.008), 6)))
            new_per_unit = (base.rate_per_unit * (Decimal("1") + drift)).quantize(
                Decimal("0.000001")
            )
            new_rate = (new_per_unit * Decimal(base.nominal)).quantize(Decimal("0.0001"))
            diff = (new_rate - base.rate).quantize(Decimal("0.0001"))
            updated = CurrencyRate(
                currency_code=base.currency_code,
                iso_numeric=base.iso_numeric,
                name_en=base.name_en,
                name_ru=base.name_ru,
                name_uz=base.name_uz,
                name_uz_cyrillic=base.name_uz_cyrillic,
                nominal=base.nominal,
                rate=new_rate,
                rate_per_unit=new_per_unit,
                diff=diff,
                rate_date=day,
            )
            rows.append(updated)
            current[ccy] = updated
        by_date[day] = rows
    return by_date


def compute_view_rows(by_date: dict[date, list[CurrencyRate]]) -> list[dict]:
    """Flatten rates into view-shaped rows with a LAG-equivalent pct_change.

    Reproduces v_rates_with_change in Python: for each currency, ordered by
    date, pct_change is the day-over-day percent change of rate_per_unit
    (NULL/None for the first observation).
    """
    per_currency: dict[str, list[CurrencyRate]] = defaultdict(list)
    for day in sorted(by_date):
        for r in by_date[day]:
            per_currency[r.currency_code].append(r)

    rows: list[dict] = []
    for ccy in sorted(per_currency):
        prev: Decimal | None = None
        for r in per_currency[ccy]:  # already date-ordered
            if prev is None or prev == 0:
                pct = None
            else:
                pct = float(((r.rate_per_unit - prev) / prev * 100).quantize(Decimal("0.0001")))
            rows.append(
                {
                    "rate_date": r.rate_date,
                    "currency_code": r.currency_code,
                    "name_en": r.name_en,
                    "name_ru": r.name_ru,
                    "nominal": r.nominal,
                    "rate": float(r.rate),
                    "rate_per_unit": float(r.rate_per_unit),
                    "diff": float(r.diff),
                    "prev_rate_per_unit": None if prev is None else float(prev),
                    "pct_change": pct,
                }
            )
            prev = r.rate_per_unit
    return rows


def _sql_str(value: str) -> str:
    """Return a single-quoted, SQL-escaped string literal."""
    return "'" + value.replace("'", "''") + "'"


def write_seed_sql(by_date: dict[date, list[CurrencyRate]], path: Path) -> None:
    """Write an idempotent SQL seed: dim_currency + fact_rates upserts."""
    currencies: dict[str, CurrencyRate] = {}
    for rates in by_date.values():
        for r in rates:
            currencies[r.currency_code] = r

    lines: list[str] = [
        "-- Auto-generated seed data. Run after schema.sql + view.",
        "-- Idempotent: re-running updates rather than duplicates.",
        "BEGIN;",
        "",
        "INSERT INTO dim_currency",
        "    (currency_code, iso_numeric, name_en, name_ru, name_uz, name_uz_cyrillic, nominal)",
        "VALUES",
    ]
    dim_values = [
        f"  ({_sql_str(r.currency_code)}, {_sql_str(r.iso_numeric)}, "
        f"{_sql_str(r.name_en)}, {_sql_str(r.name_ru)}, {_sql_str(r.name_uz)}, "
        f"{_sql_str(r.name_uz_cyrillic)}, {r.nominal})"
        for r in currencies.values()
    ]
    lines.append(",\n".join(dim_values))
    lines += [
        "ON CONFLICT (currency_code) DO UPDATE SET",
        "    iso_numeric = EXCLUDED.iso_numeric,",
        "    name_en = EXCLUDED.name_en,",
        "    name_ru = EXCLUDED.name_ru,",
        "    name_uz = EXCLUDED.name_uz,",
        "    name_uz_cyrillic = EXCLUDED.name_uz_cyrillic,",
        "    nominal = EXCLUDED.nominal;",
        "",
        "INSERT INTO fact_rates (rate_date, currency_code, rate, rate_per_unit, diff)",
        "VALUES",
    ]
    fact_values = []
    for day in sorted(by_date):
        for r in by_date[day]:
            fact_values.append(
                f"  ('{r.rate_date.isoformat()}', {_sql_str(r.currency_code)}, "
                f"{r.rate}, {r.rate_per_unit}, {r.diff})"
            )
    lines.append(",\n".join(fact_values))
    lines += [
        "ON CONFLICT (rate_date, currency_code) DO UPDATE SET",
        "    rate = EXCLUDED.rate,",
        "    rate_per_unit = EXCLUDED.rate_per_unit,",
        "    diff = EXCLUDED.diff;",
        "",
        "COMMIT;",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote %s (%d currencies, %d fact rows)", path, len(currencies), len(fact_values))


def main(argv: list[str] | None = None) -> int:
    """Generate the seed. Returns a process exit code."""
    args = parse_args(argv)
    SEED_DIR.mkdir(parents=True, exist_ok=True)

    synthetic = False
    by_date: dict[date, list[CurrencyRate]] = {}

    if args.demo:
        logger.warning("Forcing SYNTHETIC seed (--demo)")
        by_date = build_synthetic_seed(args.days)
        synthetic = True
    else:
        try:
            logger.info("Attempting live CBU fetch for the last %d days", args.days)
            by_date = fetch_live_seed(args.days)
        except requests.RequestException as exc:
            if args.live:
                logger.error("Live API unreachable and --live was set: %s", exc)
                return 1
            logger.warning("Live API unreachable (%s); falling back to SYNTHETIC", exc)
            by_date = build_synthetic_seed(args.days)
            synthetic = True

    if not by_date:
        logger.error("No data produced; aborting")
        return 1

    view_rows = compute_view_rows(by_date)
    write_parquet_from_rows(view_rows, SEED_PARQUET)
    write_parquet_from_rows(view_rows, LIVE_PARQUET)
    write_seed_sql(by_date, SEED_SQL)

    if synthetic:
        SYNTHETIC_MARKER.write_text(
            "THIS SEED IS SYNTHETIC.\n\n"
            "It was generated offline by a random walk from a single real CBU "
            "sample because the live API was unreachable at generation time.\n"
            "Do NOT publish this as real data. Before pushing to GitHub:\n"
            "  1. Delete this file (data/seed/IS_SYNTHETIC.txt).\n"
            "  2. Re-run: python scripts/generate_seed.py --live\n",
            encoding="utf-8",
        )
        logger.warning("Synthetic seed written. See %s before publishing.", SYNTHETIC_MARKER)
    elif SYNTHETIC_MARKER.exists():
        # A previous synthetic run left a marker; real data now replaces it.
        SYNTHETIC_MARKER.unlink()
        logger.info("Removed stale synthetic marker; seed is now real data")

    logger.info(
        "Seed complete (%s data, %d view rows)",
        "SYNTHETIC" if synthetic else "LIVE",
        len(view_rows),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
