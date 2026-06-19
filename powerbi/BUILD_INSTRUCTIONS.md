# Power BI — Build Instructions

This document describes how to build the **CBU Currency Dashboard** in
Power BI Desktop from the Parquet serving layer. A hand-built `.pbix` is **not**
shipped in this repository: a valid Power BI binary cannot be produced
programmatically without Power BI Desktop, and shipping a fake/corrupt `.pbix`
would be worse than shipping none. Follow the steps below to build it in about
15 minutes; then export screenshots into `docs/screenshots/`.

> Data source is the committed Parquet file served over HTTPS from GitHub.
> No gateway and no credentials are required.

---

## 0. Prerequisites

- Power BI Desktop (free).
- The repository pushed to GitHub so the raw Parquet URL resolves.
- Data source URL (replace `tuychiyevkm`):

  ```
  https://raw.githubusercontent.com/tuychiyevkm/cbu-fx-pipeline/main/data/rates.parquet
  ```

---

## 1. Apply the brand theme

1. **View → Themes → Browse for themes**.
2. Select `powerbi/theme.json` from this repo.
3. The palette is now: indigo `#1B2A4A`, teal `#17A398`, amber `#F2A93B`,
   up-green `#2ECC71`, down-red `#E15554`, background `#F7F9FC`, text `#2D2D2D`.

---

## 2. Connect to the Parquet serving layer

1. **Home → Get data → Web**.
2. Paste the raw Parquet URL from step 0. Authentication: **Anonymous**.
3. In Power Query, if the binary is not auto-recognized, use
   **Transform → Parquet** (`Parquet.Document`).
4. Set column types:
   - `rate_date` → Date
   - `currency_code`, `name_en`, `name_ru` → Text
   - `nominal` → Whole Number
   - `rate`, `rate_per_unit`, `diff`, `prev_rate_per_unit`, `pct_change` →
     Decimal Number
5. Rename the query to **`rates`**. **Close & Apply**.

> Every measure below reads pre-computed columns. **Do not** recompute
> `pct_change` in DAX — it is already calculated once in SQL and exported.

---

## 3. Minimal model & measures

Create these measures (Modeling → New measure). They only select/aggregate
pre-computed values; they never re-derive percentage change.

```DAX
Latest Date = MAX ( rates[rate_date] )

Latest Rate Per Unit =
CALCULATE (
    SELECTEDVALUE ( rates[rate_per_unit] ),
    rates[rate_date] = [Latest Date]
)

Latest Pct Change =
CALCULATE (
    SELECTEDVALUE ( rates[pct_change] ),
    rates[rate_date] = [Latest Date]
)

-- Used to color KPI values green (up) / red (down).
Pct Change Color =
SWITCH (
    TRUE (),
    [Latest Pct Change] > 0, "#2ECC71",
    [Latest Pct Change] < 0, "#E15554",
    "#F2A93B"
)
```

For the **Comparison** page (indexed-to-100), add a measure that rebases each
currency to its first value in the current date filter:

```DAX
Indexed To 100 =
VAR MinDate =
    CALCULATE ( MIN ( rates[rate_date] ), ALLSELECTED ( rates[rate_date] ) )
VAR BaseValue =
    CALCULATE (
        SELECTEDVALUE ( rates[rate_per_unit] ),
        rates[rate_date] = MinDate
    )
RETURN
    DIVIDE ( SELECTEDVALUE ( rates[rate_per_unit] ), BaseValue ) * 100
```

---

## 4. Page 1 — Overview

Layout: five KPI cards across the top, a hero line chart, a top-movers table.

1. **KPI cards (USD, EUR, RUB, GBP, CNY):**
   - Insert five **Card** visuals (or one multi-row card per currency).
   - Field: `Latest Rate Per Unit`. Add `Latest Pct Change` as a second line.
   - Filter each card to one `currency_code` (visual-level filter).
   - **Conditional formatting → Font color → Format by Field value →**
     `Pct Change Color` so the change shows green when up, red when down.
   - Card title = the currency code.
2. **USD line chart (hero visual):**
   - **Line chart**. X axis = `rate_date`. Y axis = `rate_per_unit`.
   - Visual-level filter: `currency_code = "USD"`.
   - Line color: indigo `#1B2A4A`, stroke width 3.
   - Title: "USD — UZS per 1 USD".
3. **Top movers today (top 5 by absolute % change):**
   - **Table** with `currency_code`, `name_en`, `rate_per_unit`, `pct_change`.
   - Visual-level filter: `rate_date is Latest Date` (relative/Top-N).
   - Filter **Top N = 5** by a helper measure `ABS Pct = ABS([Latest Pct Change])`.
   - Conditional-format `pct_change` cells: green/red as above.

---

## 5. Page 2 — History

1. **Currency slicer:** Slicer visual, field `currency_code` (dropdown, lists
   all ~74). Optionally show `name_en`.
2. **Date-range slicer:** Slicer visual, field `rate_date`, **Between** style.
3. **Line chart:** X = `rate_date`, Y = `rate_per_unit`, legend =
   `currency_code`. Responds to both slicers.
4. **Detail table:** columns `rate_date`, `rate_per_unit`, `pct_change`
   (straight from the view). Sort by `rate_date` descending. Conditional-format
   `pct_change` green/red.

---

## 6. Page 3 — Comparison

1. **Currency slicer (multi-select):** field `currency_code`.
2. **Date-range slicer:** field `rate_date`, Between.
3. **Indexed line chart (normalized to 100):**
   - **Line chart**. X = `rate_date`, Y = measure `Indexed To 100`,
     legend = `currency_code`.
   - This rebases every selected currency to 100 at the range start so series
     with very different magnitudes (e.g. USD vs VND) are comparable.
   - Add a constant reference line at **100**.
4. **Sortable comparison table:** columns `currency_code`, `name_en`, and a
   period-change measure:

   ```DAX
   Period Pct Change =
   VAR MinDate = CALCULATE ( MIN ( rates[rate_date] ), ALLSELECTED ( rates[rate_date] ) )
   VAR MaxDate = CALCULATE ( MAX ( rates[rate_date] ), ALLSELECTED ( rates[rate_date] ) )
   VAR First = CALCULATE ( SELECTEDVALUE ( rates[rate_per_unit] ), rates[rate_date] = MinDate )
   VAR Last  = CALCULATE ( SELECTEDVALUE ( rates[rate_per_unit] ), rates[rate_date] = MaxDate )
   RETURN DIVIDE ( Last - First, First ) * 100
   ```

   Default sort: `Period Pct Change` descending. Shows all ~74 currencies.

---

## 7. Publish & refresh

1. **File → Publish → My workspace.**
2. **Publish to web** (Embed) gives a public link for the README live demo.
3. **Scheduled refresh:** because the source is a public Web URL, no gateway is
   needed. Scheduled refresh requires **Power BI Pro** (a 60-day trial works).
   Set refresh in the Power BI Service dataset settings. Regardless of refresh
   tier, the GitHub Parquet keeps updating daily, so a manual refresh always
   pulls the latest data.

---

## 8. Screenshots

After the first data load, capture one PNG per page into `docs/screenshots/`:
`overview.png`, `history.png`, `comparison.png`. See
`docs/screenshots/README.md` for the exact shots to take.
