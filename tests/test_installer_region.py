"""Guessing where the seller sells — and refusing to guess where the product does not work."""

from __future__ import annotations

from sellee import marketplaces
from sellee.installer import region


def test_the_supported_regions_are_the_ones_the_rail_serves() -> None:
    # Every listing goes on the rail, so a country the rail has no site for is a country the
    # agent cannot sell in, whatever browser marketplaces happen to operate there.
    assert region.supported() == ["SG", "US"]
    assert marketplaces.supported_regions() == ["SG", "US"]


def test_the_currency_table_never_gets_ahead_of_the_supported_set() -> None:
    assert sorted(region.CURRENCIES) == region.supported()


def test_a_singapore_machine_is_proposed_singapore() -> None:
    assert region.guess("Asia/Singapore") == {
        "region": "SG",
        "currency": "SGD",
        "timezone": "Asia/Singapore",
    }


def test_us_zones_resolve_across_the_mainland_and_its_outliers() -> None:
    for zone in (
        "America/New_York",
        "America/Chicago",
        "America/Los_Angeles",
        "America/Anchorage",
        "Pacific/Honolulu",
        "US/Eastern",
        "America/Indiana/Indianapolis",
    ):
        assert region.region_for_zone(zone) == "US", zone


def test_other_countries_in_the_americas_are_not_guessed_as_the_us() -> None:
    # The reason US zones are listed rather than matched on an `America/` prefix: that prefix
    # also covers these, and a wrong country is not something a seller would think to check.
    for zone in ("America/Toronto", "America/Mexico_City", "America/Sao_Paulo"):
        assert region.region_for_zone(zone) is None, zone


def test_a_country_the_rail_does_not_serve_produces_no_guess() -> None:
    # Better to ask than to hand someone a confident answer the write door will refuse.
    for zone in ("Asia/Kuala_Lumpur", "Asia/Hong_Kong", "Australia/Sydney", "Europe/London"):
        assert region.guess(zone) is None, zone


def test_an_unknown_or_missing_zone_produces_no_guess() -> None:
    assert region.guess("Antarctica/Troll") is None
    assert region.guess("") is None


def test_tz_beats_the_localtime_symlink(monkeypatch) -> None:
    """A container sets TZ and leaves /etc/localtime at UTC, so reading the file proposes the
    wrong zone while the clock reads the right one. TZ overrides the machine default on a host
    too — someone who exports it means it."""
    monkeypatch.setenv("TZ", "Asia/Singapore")
    assert region.system_timezone() == "Asia/Singapore"


def test_a_tz_that_names_no_zone_falls_back_to_the_machine(monkeypatch) -> None:
    """TZ takes POSIX forms as well as zone names, and those cannot be stored or looked up."""
    for value in ("<+08>-8", "UTC+8", "Antarctica/Nowhere", "/etc/localtime"):
        monkeypatch.setenv("TZ", value)
        assert region.system_timezone() != value, value


def test_render_reads_as_the_confirmation_it_is_used_for() -> None:
    assert region.render(region.guess("Asia/Singapore")) == "SG · SGD · Asia/Singapore"
