"""Spelling a country back to the seller, and never deciding one for them."""

from __future__ import annotations

from sellee import countries
from sellee.tools.seller import validate_basics


def test_a_code_is_labelled_with_its_name() -> None:
    assert countries.label("SG") == "SG — Singapore"
    assert countries.label("sa") == "SA — Saudi Arabia"
    assert countries.label("VN") == "VN — Vietnam"


def test_a_code_with_no_name_is_still_a_country_the_agent_takes() -> None:
    """A name is not a permission. An unnamed code renders as itself and the write door still
    accepts it, because whether it can be paid out is the backend's answer, not ours."""
    assert countries.label("WW") == "WW"
    assert countries.name_for("WW") == ""
    assert validate_basics({"region": "WW"})["region"] == "WW"


def test_every_named_country_is_one_the_write_door_takes() -> None:
    for code in countries._NAMES:
        assert validate_basics({"region": code})["region"] == code


def test_a_typed_name_resolves_to_the_code_it_means() -> None:
    assert countries.code_for_name("Singapore") == "SG"
    assert countries.code_for_name("  vietnam ") == "VN"
    assert countries.code_for_name("United States") == "US"
    assert countries.code_for_name("the netherlands") == "NL"
    assert countries.code_for_name("South Korea") == "KR"


def test_a_two_letter_country_code_is_never_read_as_a_name() -> None:
    """ "AU" is Australia, not an abbreviation of anything — resolving it would be the agent
    second-guessing an answer that is already a code."""
    for code in ("AU", "SG", "US", "IT", "IN"):
        assert countries.code_for_name(code) == ""


def test_uk_resolves_because_it_is_not_a_country_code() -> None:
    """It passes the shape check, so without this it records a country that does not exist."""
    assert countries.code_for_name("UK") == "GB"
    assert "UK" not in countries._NAMES


def test_words_that_name_no_country_resolve_to_nothing() -> None:
    for text in ("", "   ", "Atlantis", "Sellerland", "somewhere nice"):
        assert countries.code_for_name(text) == ""


def test_the_names_are_not_a_list_of_countries_carousell_ai_serves() -> None:
    """Every country is here, which is what makes it spelling rather than eligibility: a list
    that excludes nobody cannot go stale the way a served-country list does."""
    assert len(countries._NAMES) > 200
    for code in ("SG", "US", "VN", "SA", "BR", "NG", "RU", "AF"):
        assert countries.name_for(code), code
