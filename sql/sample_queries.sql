-- ============================================================================
-- sample_queries.sql -- analytical showcase queries
-- ============================================================================
-- A small set of read-only queries that demonstrate window functions, ranking,
-- volatility and indexing/normalization on the CBU dataset. These power the
-- README "SQL showcase" section and double as a smoke test for the schema and
-- view. Each query is self-contained and safe to run independently.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Latest rate and daily % change for the five headline currencies.
--    Powers the Overview KPI cards. Uses the serving view directly.
-- ----------------------------------------------------------------------------
SELECT
    v.currency_code,
    v.name_en,
    v.rate_per_unit,
    v.pct_change
FROM v_rates_with_change AS v
WHERE v.rate_date = (SELECT MAX(rate_date) FROM fact_rates)
  AND v.currency_code IN ('USD', 'EUR', 'RUB', 'GBP', 'CNY')
ORDER BY array_position(ARRAY['USD','EUR','RUB','GBP','CNY'], v.currency_code);


-- ----------------------------------------------------------------------------
-- 2. Top 5 movers today by absolute daily % change.
--    Ranking with ORDER BY on a window-derived column.
-- ----------------------------------------------------------------------------
SELECT
    v.currency_code,
    v.name_en,
    v.rate_per_unit,
    v.pct_change
FROM v_rates_with_change AS v
WHERE v.rate_date = (SELECT MAX(rate_date) FROM fact_rates)
  AND v.pct_change IS NOT NULL
ORDER BY ABS(v.pct_change) DESC
LIMIT 5;


-- ----------------------------------------------------------------------------
-- 3. 30-day volatility leaderboard.
--    Standard deviation of daily % change per currency over the last 30 days,
--    ranked with RANK(). Higher stddev = more volatile.
-- ----------------------------------------------------------------------------
SELECT
    currency_code,
    name_en,
    ROUND(STDDEV_SAMP(pct_change), 4)      AS volatility_30d,
    ROUND(AVG(pct_change), 4)              AS avg_daily_pct,
    COUNT(*)                               AS observations
FROM v_rates_with_change
WHERE rate_date >= (SELECT MAX(rate_date) FROM fact_rates) - INTERVAL '30 days'
  AND pct_change IS NOT NULL
GROUP BY currency_code, name_en
HAVING COUNT(*) >= 5
ORDER BY volatility_30d DESC
LIMIT 10;


-- ----------------------------------------------------------------------------
-- 4. USD trend with a 7-day moving average.
--    Window AVG over a rolling frame -- the Overview hero line chart context.
-- ----------------------------------------------------------------------------
SELECT
    rate_date,
    rate_per_unit,
    ROUND(
        AVG(rate_per_unit) OVER (
            ORDER BY rate_date
            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
        ),
        4
    ) AS ma_7d
FROM v_rates_with_change
WHERE currency_code = 'USD'
ORDER BY rate_date DESC
LIMIT 60;


-- ----------------------------------------------------------------------------
-- 5. Indexed (normalized to 100) performance over the trailing year.
--    Each currency is rebased to 100 at its first observation in the window,
--    using FIRST_VALUE so currencies with very different magnitudes are
--    comparable on one chart. Powers the Comparison page.
-- ----------------------------------------------------------------------------
WITH windowed AS (
    SELECT
        currency_code,
        name_en,
        rate_date,
        rate_per_unit,
        FIRST_VALUE(rate_per_unit) OVER (
            PARTITION BY currency_code
            ORDER BY rate_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        ) AS base_rate
    FROM v_rates_with_change
    WHERE rate_date >= (SELECT MAX(rate_date) FROM fact_rates) - INTERVAL '1 year'
      AND currency_code IN ('USD', 'EUR', 'RUB', 'GBP', 'CNY')
)
SELECT
    currency_code,
    name_en,
    rate_date,
    rate_per_unit,
    ROUND(rate_per_unit / base_rate * 100, 2) AS indexed_value
FROM windowed
ORDER BY currency_code, rate_date;


-- ----------------------------------------------------------------------------
-- 6. Cumulative % change per currency over the selected period.
--    (last value vs first value in the window) -- sortable Comparison table.
-- ----------------------------------------------------------------------------
WITH bounds AS (
    SELECT
        currency_code,
        name_en,
        FIRST_VALUE(rate_per_unit) OVER w AS first_rate,
        LAST_VALUE(rate_per_unit)  OVER w AS last_rate
    FROM v_rates_with_change
    WHERE rate_date >= (SELECT MAX(rate_date) FROM fact_rates) - INTERVAL '90 days'
    WINDOW w AS (
        PARTITION BY currency_code
        ORDER BY rate_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    )
)
SELECT DISTINCT
    currency_code,
    name_en,
    ROUND((last_rate - first_rate) / NULLIF(first_rate, 0) * 100, 2) AS pct_change_90d
FROM bounds
ORDER BY pct_change_90d DESC NULLS LAST;
