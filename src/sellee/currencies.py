"""What a country's listings are priced in. Ported from the backend so the agent can warn a
seller before a listing exists; carousell.ai stays the authority and this only predicts it."""

from __future__ import annotations

# The ISO 4217 codes carousell.ai knows. Anything else is not a currency it will price in.
KNOWN = (
    "AUD",
    "BND",
    "CAD",
    "EUR",
    "GBP",
    "HKD",
    "IDR",
    "INR",
    "JPY",
    "KRW",
    "MYR",
    "NZD",
    "PHP",
    "PKR",
    "SGD",
    "THB",
    "TWD",
    "USD",
    "VND",
)

# A country with no entry lists in USD, which is what the backend does too. Deliberately short:
# it covers the markets Carousell trades in and nothing else.
FALLBACK = "USD"

_COUNTRY_CURRENCIES = {
    "AU": "AUD",
    "BE": "EUR",
    "BN": "BND",
    "CA": "CAD",
    "DE": "EUR",
    "GB": "GBP",
    "HK": "HKD",
    "ID": "IDR",
    "IN": "INR",
    "IT": "EUR",
    "JP": "JPY",
    "KR": "KRW",
    "MY": "MYR",
    "NL": "EUR",
    "NZ": "NZD",
    "PH": "PHP",
    "PK": "PKR",
    "SG": "SGD",
    "TH": "THB",
    "TW": "TWD",
    "US": "USD",
    "VN": "VND",
}


def for_country(code: str | None) -> str:
    """The currency a seller in this country prices in, falling back to USD. A region is named by
    its country's code and settles in that country's currency, so placement changes nothing."""
    return _COUNTRY_CURRENCIES.get(str(code or "").strip().upper(), FALLBACK)
