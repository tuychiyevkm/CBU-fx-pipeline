"""Unit tests for the CBU client parser logic.

These tests run fully offline against fixed payloads (no network). They lock
down the behaviours that matter for data correctness: string -> Decimal,
DD.MM.YYYY -> date, the nominal != 1 standardization, and the empty-response
(non-trading day) case.
"""

from datetime import date
from decimal import Decimal

import pytest
from src.cbu_client import (
    CurrencyRate,
    parse_cbu_date,
    parse_rate,
    parse_response,
)

# Verbatim real sample object from the CBU API (USD), per project spec.
SAMPLE_USD = {
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

# Real IDR object (Nominal=10) — the standardization edge case.
SAMPLE_IDR = {
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
    "Date": "18.06.2026",
}


def test_parse_rate_maps_all_fields() -> None:
    """All CBU fields map to the correct typed model attributes."""
    rate = parse_rate(SAMPLE_USD)
    assert isinstance(rate, CurrencyRate)
    assert rate.currency_code == "USD"
    assert rate.iso_numeric == "840"
    assert rate.name_en == "US Dollar"
    assert rate.name_ru == "Доллар США"
    assert rate.name_uz == "AQSH dollari"
    assert rate.name_uz_cyrillic == "АҚШ доллари"
    assert rate.nominal == 1
    assert rate.rate_date == date(2026, 5, 22)


def test_monetary_fields_are_decimal_not_float() -> None:
    """Rate and Diff must be Decimal to preserve financial precision."""
    rate = parse_rate(SAMPLE_USD)
    assert isinstance(rate.rate, Decimal)
    assert isinstance(rate.diff, Decimal)
    assert isinstance(rate.rate_per_unit, Decimal)
    assert rate.rate == Decimal("12021.22")
    assert rate.diff == Decimal("-54.96")


def test_rate_per_unit_equals_rate_when_nominal_one() -> None:
    """With Nominal=1, rate_per_unit equals the raw rate exactly."""
    rate = parse_rate(SAMPLE_USD)
    assert rate.rate_per_unit == Decimal("12021.22")


def test_nominal_ten_is_divided() -> None:
    """With Nominal=10 (IDR/IRR/VND), rate_per_unit = rate / 10."""
    rate = parse_rate(SAMPLE_IDR)
    assert rate.nominal == 10
    assert rate.rate == Decimal("6.80")
    # 6.80 / 10 must be exactly 0.68 with Decimal (no float drift).
    assert rate.rate_per_unit == Decimal("0.68")


def test_parse_cbu_date_valid() -> None:
    """DD.MM.YYYY strings parse to the correct date."""
    assert parse_cbu_date("22.05.2026") == date(2026, 5, 22)
    assert parse_cbu_date("01.01.2024") == date(2024, 1, 1)


def test_parse_cbu_date_invalid_raises() -> None:
    """A malformed date string raises ValueError."""
    with pytest.raises(ValueError):
        parse_cbu_date("2026-05-22")  # ISO format is not accepted here
    with pytest.raises(ValueError):
        parse_cbu_date("not-a-date")


def test_parse_response_empty_returns_empty_list() -> None:
    """An empty array (non-trading day) parses to an empty list, no error."""
    assert parse_response([]) == []


def test_parse_response_skips_malformed_records() -> None:
    """A bad record is skipped; valid records in the same payload survive."""
    payload = [SAMPLE_USD, {"Ccy": "BAD"}]  # second lacks required fields
    rates = parse_response(payload)
    assert len(rates) == 1
    assert rates[0].currency_code == "USD"


def test_parse_response_rejects_non_list() -> None:
    """A non-array payload raises ValueError."""
    with pytest.raises(ValueError):
        parse_response({"not": "a list"})  # type: ignore[arg-type]


def test_invalid_nominal_raises() -> None:
    """Nominal of zero or negative is rejected (avoids divide-by-zero)."""
    bad = dict(SAMPLE_USD, Nominal="0")
    with pytest.raises(ValueError):
        parse_rate(bad)


def test_unparseable_rate_raises() -> None:
    """A non-numeric Rate raises ValueError."""
    bad = dict(SAMPLE_USD, Rate="abc")
    with pytest.raises(ValueError):
        parse_rate(bad)
