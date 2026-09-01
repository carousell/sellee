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


def test_a_mac_reports_its_zone_rather_than_nothing(monkeypatch) -> None:
    """macOS points /etc/localtime at zoneinfo.default, so looking for a literal "/zoneinfo/"
    found nothing on every Mac."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        region.os.path, "realpath", lambda _: "/usr/share/zoneinfo.default/Asia/Singapore"
    )
    assert region.system_timezone() == "Asia/Singapore"
    assert region.guess() == {"region": "SG", "currency": "SGD", "timezone": "Asia/Singapore"}


def test_the_plain_zoneinfo_layout_still_reads(monkeypatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        region.os.path, "realpath", lambda _: "/usr/share/zoneinfo/America/New_York"
    )
    assert region.system_timezone() == "America/New_York"


def test_a_localtime_path_naming_no_zone_reports_nothing(monkeypatch) -> None:
    """Widened matching must not turn a path we cannot read into a confident wrong answer."""
    monkeypatch.delenv("TZ", raising=False)
    for resolved in ("/etc/localtime", "/usr/share/zoneinfo/Nowhere/Fake", "/var/db/zoneinfo"):
        monkeypatch.setattr(region.os.path, "realpath", lambda _, r=resolved: r)
        assert region.system_timezone() == "", resolved


def test_the_zone_name_is_read_from_the_last_database_directory(monkeypatch) -> None:
    """A home directory of one's own called `zoneinfo` cannot claim the rest of the path."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        region.os.path, "realpath", lambda _: "/home/zoneinfo/share/zoneinfo/Asia/Singapore"
    )
    assert region.system_timezone() == "Asia/Singapore"


def test_zones_for_is_the_reverse_of_the_lookup() -> None:
    assert region.zones_for("SG") == ["Asia/Singapore"]
    assert region.zones_for("US")[0] == "America/New_York"
    assert all(region.region_for_zone(zone) == "US" for zone in region.zones_for("US"))


def test_a_silent_machine_still_proposes_the_zone_of_a_one_zone_country() -> None:
    # A one-zone country leaves nothing to ask, even when the machine says nothing.
    assert region.default_zone("SG", "") == "Asia/Singapore"


def test_a_country_with_several_zones_proposes_none() -> None:
    # Guessing New York for a seller in Denver is a wrong default, not a helpful one.
    assert region.default_zone("US", "") == ""


def test_the_machines_own_zone_beats_the_country_default() -> None:
    """The stored zone is a claim about this machine — the clock check compares it against this
    process's clock — so where the seller *is* wins over where they sell."""
    assert region.default_zone("SG", "Asia/Kuala_Lumpur") == "Asia/Kuala_Lumpur"
    assert region.default_zone("US", "America/Denver") == "America/Denver"


def test_zone_error_names_what_is_wrong_and_passes_what_is_right() -> None:
    assert region.zone_error("Asia/Singapore") == ""
    assert region.zone_error("America/Indiana/Indianapolis") == ""
    assert "gmt8+" in region.zone_error("gmt8+")
    assert region.zone_error("")
    assert region.zone_error("../../etc/passwd")


def test_zone_error_is_never_stricter_than_the_write_door() -> None:
    """Setup checks locally so a typo re-asks; the door stays the authority, so a local check
    must not refuse what it accepts."""
    from sellee.tools.seller import BasicsError, validate_basics

    for name in ("Asia/Singapore", "America/New_York", "gmt8+", "UTC+8", "../../etc/passwd"):
        refused_here = bool(region.zone_error(name))
        try:
            validate_basics({"timezone": name})
            refused_there = False
        except BasicsError:
            refused_there = True
        assert refused_here == refused_there, name
