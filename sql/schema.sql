-- ============================================================================
-- CBU FX pipeline -- star-schema DDL (PostgreSQL / Supabase)
-- ============================================================================
-- Two tables form a classic star schema:
--   dim_currency : one row per currency (descriptive attributes)
--   fact_rates   : one row per (date, currency) measurement
-- This script is idempotent: it can be run repeatedly without error and is
-- safe to apply on a fresh Supabase project or an existing one.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Dimension: currency
-- ----------------------------------------------------------------------------
-- One row per currency. `nominal` is the quote unit and is NOT always 1
-- (CBU quotes IDR, IRR and VND per 10 units). It is kept here so the fact's
-- standardized `rate_per_unit` can always be re-derived if needed.
CREATE TABLE IF NOT EXISTS dim_currency (
    currency_code    VARCHAR(3)  PRIMARY KEY,   -- ISO alpha-3, CBU "Ccy"
    iso_numeric      VARCHAR(3)  NOT NULL,       -- ISO numeric, CBU "Code"
    name_en          TEXT        NOT NULL,
    name_ru          TEXT        NOT NULL,
    name_uz          TEXT        NOT NULL,
    name_uz_cyrillic TEXT        NOT NULL,
    nominal          INTEGER     NOT NULL DEFAULT 1
                                 CHECK (nominal > 0)
);

COMMENT ON TABLE  dim_currency           IS 'Currency dimension (one row per currency).';
COMMENT ON COLUMN dim_currency.nominal   IS 'Quote unit; NOT always 1 (10 for IDR/IRR/VND).';

-- ----------------------------------------------------------------------------
-- Fact: daily rates
-- ----------------------------------------------------------------------------
-- One row per currency per date. Monetary columns use NUMERIC (exact) rather
-- than floating point. `rate_per_unit` = rate / nominal is stored so the
-- comparison page never has to divide, and `diff` carries CBU's own daily
-- absolute change. The UNIQUE(rate_date, currency_code) constraint backs the
-- idempotent upsert (INSERT ... ON CONFLICT).
CREATE TABLE IF NOT EXISTS fact_rates (
    id            BIGSERIAL      PRIMARY KEY,
    rate_date     DATE           NOT NULL,
    currency_code VARCHAR(3)     NOT NULL
                                 REFERENCES dim_currency (currency_code),
    rate          NUMERIC(18, 4) NOT NULL,   -- raw quoted rate for `nominal` units
    rate_per_unit NUMERIC(18, 6) NOT NULL,   -- rate / nominal (standardized)
    diff          NUMERIC(18, 4) NOT NULL,   -- CBU's own daily absolute change
    CONSTRAINT uq_fact_rates_date_currency UNIQUE (rate_date, currency_code)
);

COMMENT ON TABLE  fact_rates               IS 'Daily exchange-rate facts (one row per date+currency).';
COMMENT ON COLUMN fact_rates.rate_per_unit IS 'rate / nominal -- standardized, comparable across currencies.';
COMMENT ON COLUMN fact_rates.diff          IS 'CBU-reported absolute daily change.';

-- Index supporting the LAG window function in v_rates_with_change and the
-- typical History-page filter (one currency over a date range).
CREATE INDEX IF NOT EXISTS ix_fact_rates_currency_date
    ON fact_rates (currency_code, rate_date);

-- Index supporting per-date queries (Overview / Top movers "today").
CREATE INDEX IF NOT EXISTS ix_fact_rates_date
    ON fact_rates (rate_date);
