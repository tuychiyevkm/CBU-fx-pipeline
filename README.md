# CBU Currency Dashboard — `cbu-fx-pipeline`

**Language:** **English** · [Русский](README.ru.md)

> An end-to-end data pipeline that collects daily currency exchange rates from
> the Central Bank of Uzbekistan (CBU) public API, loads them into a PostgreSQL
> star schema, computes day-over-day changes in SQL, and serves a Parquet file
> to Power BI — refreshed automatically every day via GitHub Actions.

[![CI](https://github.com/USERNAME/cbu-fx-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/USERNAME/cbu-fx-pipeline/actions/workflows/ci.yml)
[![Daily fetch](https://github.com/USERNAME/cbu-fx-pipeline/actions/workflows/daily-fetch.yml/badge.svg)](https://github.com/USERNAME/cbu-fx-pipeline/actions/workflows/daily-fetch.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://github.com/astral-sh/ruff)

**Live demo:** _add your Power BI "Publish to web" link here_
**Dashboard preview:** see [`docs/screenshots/`](docs/screenshots/) (added after first run).

---

## Problem

The CBU publishes official exchange rates for ~74 currencies through a public
JSON API, but only as a daily snapshot — there is no convenient historical,
analysis-ready store and no automatic refresh. This project turns that raw feed
into a clean, queryable warehouse and a self-updating BI dashboard, so trends,
volatility and cross-currency comparisons are one click away.

---

## Architecture

```
CBU JSON API
   │  (Python ETL: fetch_daily.py / backfill.py)
   ▼
Supabase PostgreSQL  ──►  star schema: dim_currency + fact_rates
   │                       view: v_rates_with_change (LAG window function)
   │  (export step writes the view output)
   ▼
data/rates.parquet  ──(committed to repo, served via)──►  raw.githubusercontent.com
   ▼
Power BI (Web connector)  ──►  scheduled refresh works, NO gateway
```

### Why a warehouse + serving split

This is a deliberate, real-world pattern, not a workaround:

- **Heavy SQL lives in PostgreSQL.** The star schema, window functions and the
  day-over-day percentage change are all computed in the database — the right
  place for set-based analytics.
- **A friction-free serving layer feeds Power BI.** Connecting Power BI
  directly to a cloud Postgres requires an on-premises data gateway and brings
  TLS/certificate headaches for scheduled refresh. Instead, the pipeline
  exports the analytical view to a single Parquet file, commits it, and serves
  it over plain HTTPS from `raw.githubusercontent.com`. Power BI reads it with
  the Web connector — **no gateway, no certificate problems**, and scheduled
  refresh "just works".

---

## Tech stack

| Layer        | Choice                                                       |
|--------------|--------------------------------------------------------------|
| Language     | Python 3.12                                                  |
| Libraries    | `requests`, `psycopg2-binary`, `python-dotenv`, `pyarrow`, `pandas` |
| Warehouse    | PostgreSQL on Supabase (free tier)                          |
| Serving      | Parquet file served via GitHub raw URL                      |
| BI           | Power BI (Web connector → Parquet)                          |
| Automation   | GitHub Actions (primary) + local cron / Task Scheduler (alt) |
| Quality      | `ruff` (lint + format), `pytest`                            |

---

## Data model

A classic **star schema** with one dimension and one fact, plus a serving view.

**`dim_currency`** — one row per currency:
`currency_code` (PK), `iso_numeric`, `name_en`, `name_ru`, `name_uz`,
`name_uz_cyrillic`, `nominal`.

**`fact_rates`** — one row per (date, currency):
`id` (PK), `rate_date`, `currency_code` (FK), `rate` `NUMERIC(18,4)`,
`rate_per_unit` `NUMERIC(18,6)`, `diff` `NUMERIC(18,4)`,
`UNIQUE(rate_date, currency_code)`.

**`v_rates_with_change`** — joins fact + dim and uses
`LAG(rate_per_unit) OVER (PARTITION BY currency_code ORDER BY rate_date)` to
produce `pct_change`, the daily percentage change of the standardized rate.

### Two modeling details that matter

- **`nominal` is not always 1.** CBU quotes IDR, IRR and VND **per 10 units**.
  The pipeline stores `rate_per_unit = rate / nominal` so every currency is
  directly comparable; otherwise the Comparison page would be wrong by an order
  of magnitude.
- **Percentage change is computed once, in SQL — never in DAX.** It is
  calculated in the view during ETL and exported to Parquet. Computing it once
  at the source (a) keeps the metric identical everywhere it is consumed,
  (b) avoids re-deriving it on every slicer interaction in Power BI, and
  (c) makes the Parquet file a portable, self-contained serving layer.

---

## Setup

### 1. Create the Supabase project and get the pooler connection string

1. Create a free project at [supabase.com](https://supabase.com).
2. **Project Settings → Database → Connection string → Session pooler.**
   Copy that string. It uses an **IPv4** host and the username form
   `postgres.<project-ref>`:

   ```
   postgresql://postgres.<project-ref>:<password>@<region>.pooler.supabase.com:5432/postgres
   ```

   > Use the **Session Pooler**, not the direct connection. The direct
   > connection is IPv6-only, and GitHub Actions runners are IPv4-only — so the
   > direct string fails in CI. The pooler host is IPv4.

### 2. Configure environment & install

```bash
cp .env.example .env          # then paste your DATABASE_URL into .env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Create the schema and view

Run the SQL in the Supabase SQL editor (or via `psql`):

```bash
psql "$DATABASE_URL" -f sql/schema.sql
psql "$DATABASE_URL" -f sql/view_rates_with_change.sql
```

(`fetch_daily.py` and `backfill.py` also bootstrap these automatically.)

### 4. Backfill two years of history

```bash
python -m src.backfill            # last 2 years, resume-safe, polite delay
```

The backfill skips non-trading days, skips dates already loaded, and tolerates
individual failed requests without aborting.

### 5. Generate the seed (for the public repo)

```bash
python scripts/generate_seed.py   # fetches the last 30 days from the live API
```

> **If a synthetic seed was shipped:** this repo may include a clearly-labeled
> synthetic seed (`data/seed/IS_SYNTHETIC.txt` present) generated offline so the
> project is demonstrable without network access. **Before publishing, delete
> `data/seed/IS_SYNTHETIC.txt` and re-run `python scripts/generate_seed.py
> --live`** so the committed seed contains real CBU data.

---

## Automation

### GitHub Actions (primary)

Two workflows:

- **`ci.yml`** — on every push/PR: `ruff check`, `ruff format --check`,
  `pytest` on Python 3.12.
- **`daily-fetch.yml`** — `cron: "0 7 * * *"` (= **12:00 Asia/Tashkent**, after
  the CBU publishes) plus manual `workflow_dispatch`. It runs `fetch_daily`,
  regenerates `data/rates.parquet`, and commits it back if it changed.

**Required repository secret:**

| Secret name    | Value                                                       |
|----------------|-------------------------------------------------------------|
| `DATABASE_URL` | The Supabase **Session Pooler (IPv4)** connection string.  |

Set it in **Settings → Secrets and variables → Actions → New repository
secret**. Nothing is ever hardcoded.

### Local cron / Task Scheduler (documented alternative)

**Linux/macOS cron** (12:00 Tashkent = 07:00 UTC):

```cron
0 7 * * * cd /path/to/cbu-fx-pipeline && /path/to/.venv/bin/python -m src.fetch_daily >> fetch.log 2>&1
```

**Windows Task Scheduler:** create a daily task at 12:00 local time running
`python -m src.fetch_daily` in the project directory, with `DATABASE_URL` set in
the environment.

---

## Power BI

The dashboard reads the Parquet serving file over HTTPS — no gateway required.

1. **Get data → Web**, URL:
   `https://raw.githubusercontent.com/USERNAME/cbu-fx-pipeline/main/data/rates.parquet`
   (Anonymous auth).
2. **View → Themes → Browse** and apply [`powerbi/theme.json`](powerbi/theme.json).
3. Build the three pages following
   [`powerbi/BUILD_INSTRUCTIONS.md`](powerbi/BUILD_INSTRUCTIONS.md):
   - **Overview** — KPI cards (USD, EUR, RUB, GBP, CNY) with green-up/red-down
     change, USD hero line chart, top-5 movers today.
   - **History** — currency slicer (all ~74), date-range slicer, line chart,
     detail table.
   - **Comparison** — several currencies indexed to 100 at the range start,
     plus a sortable %-change table.
4. Capture screenshots into `docs/screenshots/` (see its README).
5. **Publish to web** for the live-demo link.

> A binary `.pbix` is **not** shipped: a valid one cannot be produced
> programmatically, and a corrupt placeholder would be worse than none. The
> build instructions reproduce the report in ~15 minutes. **Scheduled refresh**
> in the Power BI Service requires **Power BI Pro** (a 60-day trial covers it);
> regardless of refresh tier, the GitHub Parquet keeps updating daily, so a
> manual refresh always pulls the latest data.

### Brand palette

| Role        | Hex       |
|-------------|-----------|
| Primary     | `#1B2A4A` |
| Accent      | `#17A398` |
| Highlight   | `#F2A93B` |
| Up (green)  | `#2ECC71` |
| Down (red)  | `#E15554` |
| Background  | `#F7F9FC` |
| Text        | `#2D2D2D` |

---

## SQL showcase

Day-over-day change via a `LAG` window function (the core of the serving view):

```sql
SELECT
    rate_date,
    currency_code,
    rate_per_unit,
    ROUND(
        (rate_per_unit - LAG(rate_per_unit) OVER w)
        / LAG(rate_per_unit) OVER w * 100, 4
    ) AS pct_change
FROM fact_rates
WINDOW w AS (PARTITION BY currency_code ORDER BY rate_date);
```

30-day volatility leaderboard (standard deviation of daily % change, ranked):

```sql
SELECT
    currency_code,
    ROUND(STDDEV_SAMP(pct_change), 4) AS volatility_30d
FROM v_rates_with_change
WHERE rate_date >= (SELECT MAX(rate_date) FROM fact_rates) - INTERVAL '30 days'
  AND pct_change IS NOT NULL
GROUP BY currency_code
ORDER BY volatility_30d DESC
LIMIT 10;
```

More in [`sql/sample_queries.sql`](sql/sample_queries.sql) (moving averages,
indexed normalization, top movers).

---

## Project structure

```
cbu-fx-pipeline/
├── src/
│   ├── cbu_client.py        # HTTP + parsing (Decimal, DD.MM.YYYY, nominal)
│   ├── database.py          # psycopg2 (Session Pooler), upsert, export-to-parquet
│   ├── fetch_daily.py       # entry point: fetch today, upsert, re-export
│   └── backfill.py          # entry point: 2-year backfill, resume-safe
├── scripts/generate_seed.py # 30-day live seed (+ --demo synthetic fallback)
├── sql/                     # schema.sql, view, sample_queries.sql
├── tests/                   # parser + upsert idempotency tests
├── powerbi/                 # theme.json + BUILD_INSTRUCTIONS.md
├── data/                    # rates.parquet (serving) + seed/
├── docs/screenshots/        # dashboard screenshots (added after first run)
├── .github/workflows/       # ci.yml + daily-fetch.yml
├── .env.example
├── requirements.txt
├── pyproject.toml
└── LICENSE
```

---

## License

[MIT](LICENSE) © 2026 Komron Toychiyev.

Exchange-rate data © Central Bank of the Republic of Uzbekistan,
via the public [cbu.uz](https://cbu.uz) API.
