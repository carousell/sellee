"""The currency a country's listings are priced in — the agent's copy of bazaar's answer."""

from __future__ import annotations

from sellee import currencies


def test_the_set_is_the_nineteen_codes_bazaar_knows() -> None:
    assert currencies.KNOWN == (
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


def test_a_country_in_the_table_prices_in_its_own_currency() -> None:
    for code, expected in (
        ("SG", "SGD"),
        ("US", "USD"),
        ("VN", "VND"),
        ("MY", "MYR"),
        ("JP", "JPY"),
        ("TW", "TWD"),
        ("HK", "HKD"),
        ("ID", "IDR"),
        ("PH", "PHP"),
        ("TH", "THB"),
        ("KR", "KRW"),
        ("IN", "INR"),
        ("PK", "PKR"),
        ("AU", "AUD"),
        ("NZ", "NZD"),
        ("CA", "CAD"),
        ("GB", "GBP"),
        ("BN", "BND"),
    ):
        assert currencies.for_country(code) == expected, code


def test_the_euro_countries_share_one_code() -> None:
    for code in ("DE", "NL", "BE", "IT"):
        assert currencies.for_country(code) == "EUR", code


def test_a_country_outside_the_table_prices_in_usd() -> None:
    # bazaar's fallback, and the reason it is safe to predict: a country with no entry lists in
    # USD there too, so the agent and the backend agree rather than merely both guessing.
    for code in ("BR", "CN", "NG", "TR", "KE", "AE", "ZZ", ""):
        assert currencies.for_country(code) == "USD", code


def test_a_country_code_is_read_however_it_was_typed() -> None:
    assert currencies.for_country("vn") == "VND"
    assert currencies.for_country(" sg ") == "SGD"
    assert currencies.for_country(None) == "USD"


def test_every_two_letter_code_resolves_to_a_code_in_the_set() -> None:
    alphabet = [chr(point) for point in range(ord("A"), ord("Z") + 1)]
    for first in alphabet:
        for second in alphabet:
            assert currencies.for_country(first + second) in currencies.KNOWN


def test_the_settleable_codes_are_the_two_regions_carousell_ai_runs() -> None:
    # SG settles in SGD and US in USD, and a region is named by its country's code, so a placed
    # seller's region currency and their country's are the same answer.
    assert currencies.for_country("SG") == "SGD"
    assert currencies.for_country("US") == "USD"
