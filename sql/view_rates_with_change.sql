-- ============================================================================
-- View: v_rates_with_change
-- ============================================================================
-- The single serving view. It joins the fact to the currency dimension and
-- uses a LAG window function to compute the day-over-day percentage change of
-- the standardized rate (rate_per_unit), partitioned per currency and ordered
-- by date.
--
-- Design decision: the percentage change is computed HERE, in SQL, once during
-- ETL, and then exported to Parquet. It is deliberately NOT recomputed in
-- Power BI / DAX. Computing it once at the source keeps the metric consistent
-- everywhere it is consumed, avoids re-deriving it on every slicer interaction,
-- and makes the Parquet file a self-contained, portable serving layer.
--
-- pct_change is NULL for the first observed date of each currency (no prior
-- row to compare against) and when the previous standardized rate is 0.
-- ============================================================================

CREATE OR REPLACE VIEW v_rates_with_change AS
SELECT
    f.rate_date,
    f.currency_code,
    d.name_en,
    d.name_ru,
    d.nominal,
    f.rate,
    f.rate_per_unit,
    f.diff,
    -- Previous day's standardized rate for this currency (NULL on first day).
    LAG(f.rate_per_unit) OVER w AS prev_rate_per_unit,
    -- Daily % change of the standardized rate. Guard against divide-by-zero:
    -- when the previous value is NULL or 0, the result is NULL.
    CASE
        WHEN LAG(f.rate_per_unit) OVER w IS NULL THEN NULL
        WHEN LAG(f.rate_per_unit) OVER w = 0    THEN NULL
        ELSE ROUND(
            (f.rate_per_unit - LAG(f.rate_per_unit) OVER w)
            / LAG(f.rate_per_unit) OVER w * 100,
            4
        )
    END AS pct_change
FROM fact_rates AS f
JOIN dim_currency AS d
    ON d.currency_code = f.currency_code
WINDOW w AS (PARTITION BY f.currency_code ORDER BY f.rate_date);

COMMENT ON VIEW v_rates_with_change IS
    'Serving view: fact + dim with LAG-based daily pct_change of rate_per_unit. '
    'Percentage change is computed once here in SQL and exported to Parquet; '
    'it is never recomputed in Power BI.';
