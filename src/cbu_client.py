"""HTTP client and parser for the CBU currency exchange-rate API.

This module is the single source of truth for talking to the Central Bank
of Uzbekistan (CBU) public JSON API and for turning its raw, all-strings
payload into clean, typed Python objects. It is imported by both entry
points (``fetch_daily.py`` and ``backfill.py``) so that fetching and parsing
behave identically everywhere.

Key responsibilities:
    * Build the correct daily / historical endpoint URLs.
    * Perform HTTP GET requests with a timeout and a descriptive User-Agent.
    * Parse the response defensively: every value in the CBU payload is a
      JSON string, so monetary fields are converted to ``Decimal`` (never
      ``float``) and the ``DD.MM.YYYY`` date is converted to ``date``.
    * Compute ``rate_per_unit = rate / nominal`` so currencies quoted per
      10 units (IDR, IRR, VND) are comparable with the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Public CBU JSON API. The /uz/ locale is used for BOTH endpoints so the
# CcyNm_UZ / CcyNm_UZC fields are always populated.
DAILY_URL = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/"
HISTORICAL_URL_TEMPLATE = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/all/{date}/"

# A polite, identifiable User-Agent. Some public APIs reject the default
# requests UA, and an identifiable one is good citizenship for backfills.
USER_AGENT = "cbu-fx-pipeline/1.0 (+https://github.com/USERNAME/cbu-fx-pipeline)"

DEFAULT_TIMEOUT = 30  # seconds


@dataclass(frozen=True, slots=True)
class CurrencyRate:
    """A single parsed CBU rate record for one currency on one date.

    All monetary values are ``Decimal`` to preserve financial precision.
    ``rate_per_unit`` is the standardized comparable value (rate / nominal).
    """

    currency_code: str  # e.g. "USD" (CBU "Ccy")
    iso_numeric: str  # e.g. "840" (CBU "Code")
    name_en: str
    name_ru: str
    name_uz: str
    name_uz_cyrillic: str
    nominal: int  # quote unit; NOT always 1 (10 for IDR/IRR/VND)
    rate: Decimal  # raw quoted rate for `nominal` units
    rate_per_unit: Decimal  # rate / nominal — standardized, comparable
    diff: Decimal  # CBU's own daily absolute change
    rate_date: date  # parsed from CBU "Date" (DD.MM.YYYY)


def _to_decimal(value: Any, field: str) -> Decimal:
    """Convert a raw CBU string value to ``Decimal``.

    The CBU API returns every numeric value as a string (e.g. "12021.22").
    Using ``Decimal`` avoids binary floating-point rounding errors on money.

    Raises:
        ValueError: if the value cannot be parsed as a decimal number.
    """
    try:
        # str() guards against the rare case where a value arrives non-string.
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError) as exc:
        raise ValueError(f"Cannot parse {field!r} as Decimal: {value!r}") from exc


def _to_int(value: Any, field: str) -> int:
    """Convert a raw CBU string value to ``int`` (used for Nominal)."""
    try:
        return int(str(value).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Cannot parse {field!r} as int: {value!r}") from exc


def parse_cbu_date(value: str) -> date:
    """Parse a CBU date string in ``DD.MM.YYYY`` format into a ``date``.

    Example:
        >>> parse_cbu_date("22.05.2026")
        datetime.date(2026, 5, 22)

    Raises:
        ValueError: if the string does not match the expected format.
    """
    try:
        return datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot parse CBU date {value!r} (expected DD.MM.YYYY)") from exc


def parse_rate(item: dict[str, Any]) -> CurrencyRate:
    """Parse one raw CBU JSON object into a typed :class:`CurrencyRate`.

    Field mapping (CBU key -> model field):
        Ccy        -> currency_code
        Code       -> iso_numeric
        CcyNm_EN   -> name_en
        CcyNm_RU   -> name_ru
        CcyNm_UZ   -> name_uz
        CcyNm_UZC  -> name_uz_cyrillic
        Nominal    -> nominal (int)
        Rate       -> rate (Decimal)
        Diff       -> diff (Decimal)
        Date       -> rate_date (date, from DD.MM.YYYY)

    ``rate_per_unit`` is computed as ``rate / nominal`` so that currencies
    quoted per 10 units (IDR, IRR, VND) are directly comparable with those
    quoted per 1 unit.

    Raises:
        KeyError: if a required field is missing from the payload.
        ValueError: if a field cannot be parsed to its target type.
    """
    nominal = _to_int(item["Nominal"], "Nominal")
    if nominal <= 0:
        raise ValueError(f"Invalid Nominal (must be > 0): {item.get('Nominal')!r}")

    rate = _to_decimal(item["Rate"], "Rate")
    rate_per_unit = rate / Decimal(nominal)

    return CurrencyRate(
        currency_code=str(item["Ccy"]).strip(),
        iso_numeric=str(item["Code"]).strip(),
        name_en=str(item["CcyNm_EN"]).strip(),
        name_ru=str(item["CcyNm_RU"]).strip(),
        name_uz=str(item["CcyNm_UZ"]).strip(),
        name_uz_cyrillic=str(item["CcyNm_UZC"]).strip(),
        nominal=nominal,
        rate=rate,
        rate_per_unit=rate_per_unit,
        diff=_to_decimal(item["Diff"], "Diff"),
        rate_date=parse_cbu_date(item["Date"]),
    )


def parse_response(payload: list[dict[str, Any]]) -> list[CurrencyRate]:
    """Parse a full CBU JSON array into a list of :class:`CurrencyRate`.

    An empty payload (``[]``) is valid and returns an empty list; this is how
    the historical endpoint signals a non-trading day (weekend/holiday).
    Individual malformed records are logged and skipped so that one bad row
    does not discard an otherwise valid response.
    """
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array, got {type(payload).__name__}")

    rates: list[CurrencyRate] = []
    for item in payload:
        try:
            rates.append(parse_rate(item))
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed CBU record: %s", exc)
    return rates


def fetch_daily(session: requests.Session | None = None) -> list[CurrencyRate]:
    """Fetch and parse the latest published rates for all currencies.

    Returns:
        A list of parsed :class:`CurrencyRate` for the most recent CBU
        publication date (typically all ~74 currencies).
    """
    return _get_and_parse(DAILY_URL, session=session)


def fetch_historical(target: date, session: requests.Session | None = None) -> list[CurrencyRate]:
    """Fetch and parse all currency rates for a specific historical date.

    Args:
        target: the date to fetch (the URL uses ISO ``YYYY-MM-DD``).

    Returns:
        A list of parsed :class:`CurrencyRate`. On a non-trading day the CBU
        endpoint returns an empty array, so this returns an empty list.
    """
    url = HISTORICAL_URL_TEMPLATE.format(date=target.isoformat())
    return _get_and_parse(url, session=session)


def _get_and_parse(url: str, session: requests.Session | None = None) -> list[CurrencyRate]:
    """Perform a GET request against ``url`` and parse the JSON response.

    A caller-supplied :class:`requests.Session` is reused when provided
    (important for the ~730-call backfill); otherwise a one-off request is
    made.

    Raises:
        requests.RequestException: on network/HTTP errors (caller handles).
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    logger.debug("GET %s", url)
    requester = session.get if session is not None else requests.get
    response = requester(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return parse_response(response.json())
