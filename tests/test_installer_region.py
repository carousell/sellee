"""Guessing where the seller sells, and never deciding it."""

from __future__ import annotations

import os
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sellee.installer import region


def test_a_singapore_machine_is_proposed_singapore() -> None:
    # No currency in the proposal: what a listing is priced in comes from the backend.
    assert region.guess("Asia/Singapore") == {"region": "SG", "timezone": "Asia/Singapore"}


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
    # Toronto resolves, but to Canada — the prefix still decides nothing.
    assert region.region_for_zone("America/Toronto") == "CA"
    for zone in ("America/Mexico_City", "America/Sao_Paulo"):
        assert region.region_for_zone(zone) is None, zone


def test_the_countries_a_seller_is_likeliest_to_be_in_are_all_guessable() -> None:
    """Setup proposes rather than asking wherever the machine can vouch for the answer, and the
    markets carousell.ai trades in are the ones that has to cover."""
    guessable = set(region._ZONE_REGIONS.values())
    guessable |= {code for _, code in region._ZONE_PREFIX_REGIONS}
    for code in ("AU", "BN", "CA", "DE", "GB", "HK", "ID", "IN", "IT", "JP"):
        assert code in guessable, code
    for code in ("KR", "MY", "NL", "NZ", "PH", "PK", "SG", "TH", "TW", "US", "VN"):
        assert code in guessable, code
    assert region.region_for_zone("Europe/Brussels") == "BE"
    # Every guessable country also has a zone to propose once it is known, so the timezone
    # question never offers an example from the wrong side of the world.
    for code in guessable:
        assert region.zones_for(code), code


def test_australia_is_a_prefix_because_every_zone_under_it_is_australian() -> None:
    for zone in ("Australia/Sydney", "Australia/Perth", "Australia/Darwin", "Australia/Eucla"):
        assert region.region_for_zone(zone) == "AU", zone


def test_a_zone_the_table_does_not_name_produces_no_guess() -> None:
    # A guess is a convenience, so an absent country asks rather than proposing. Nothing here
    # decides where the seller may sell: the answer they type is accepted whatever it is.
    for zone in ("Asia/Riyadh", "Africa/Lagos", "Europe/Madrid", "America/Bogota"):
        assert region.guess(zone) is None, zone


def test_the_confirm_line_shows_only_what_is_recorded() -> None:
    # Before registration answers there is no currency to show, and predicting one here was the
    # ported table this change deleted.
    assert region.render({"region": "SG", "timezone": "Asia/Singapore"}) == (
        "SG — Singapore · Asia/Singapore"
    )
    assert region.render({"region": "BR"}) == "BR — Brazil"
    # A recorded currency is shown as recorded.
    assert region.render({"region": "SG", "currency": "SGD"}) == "SG — Singapore · SGD"
    # A code no name is known for still renders: it is a country the agent accepts either way.
    assert region.render({"region": "WW"}) == "WW"


def test_the_confirm_line_names_the_country_so_a_code_can_be_proofread() -> None:
    """The SA/SG class: two codes of identical shape, both real, one wrong. The name is what
    makes the difference visible while the seller can still correct it."""
    assert region.render({"region": "SA"}).startswith("SA — Saudi Arabia")
    assert region.render({"region": "SG"}).startswith("SG — Singapore")


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
    # A guess carries no currency, so the confirmation names country and zone alone.
    assert region.render(region.guess("Asia/Singapore")) == "SG — Singapore · Asia/Singapore"


def test_a_mac_reports_its_zone_rather_than_nothing(monkeypatch) -> None:
    """macOS points /etc/localtime at zoneinfo.default, so looking for a literal "/zoneinfo/"
    found nothing on every Mac."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        region.os.path, "realpath", lambda _: "/usr/share/zoneinfo.default/Asia/Singapore"
    )
    assert region.system_timezone() == "Asia/Singapore"
    assert region.guess() == {"region": "SG", "timezone": "Asia/Singapore"}


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


# --- the timezone as a place ---------------------------------------------------------------------

_ZONE_TAB = """\
# tz zone descriptions (fixture)
ES\t+4024-00341\tEurope/Madrid\tSpain (mainland)
ES\t+3553-00519\tAfrica/Ceuta\tCeuta, Melilla
ES\t+2806-01524\tAtlantic/Canary\tCanary Islands
FR\t+4852+00220\tEurope/Paris
PT\t+3843-00908\tEurope/Lisbon\tPortugal (mainland)
PT\t+3238-01654\tAtlantic/Madeira\tMadeira Islands
PT\t+3744-02540\tAtlantic/Azores\tAzores
US\t+404251-0740023\tAmerica/New_York\tEastern (most areas)
US\t+394606-0860929\tAmerica/Indiana/Indianapolis\tEastern - IN (most areas)
IN\t+2232+08822\tAsia/Kolkata
XX\t+0000+00000\tNowhere/Atlantis\tA zone no database has
"""


@pytest.fixture
def zone_tab(tmp_path):
    path = tmp_path / "zone.tab"
    path.write_text(_ZONE_TAB)
    return str(path)


def test_a_country_the_table_names_is_offered_as_its_cities_most_populous_first(zone_tab) -> None:
    places = region.place_zones("US", zone_tab)
    assert places[:4] == [
        ("New York", "America/New_York"),
        ("Chicago", "America/Chicago"),
        ("Denver", "America/Denver"),
        ("Los Angeles", "America/Los_Angeles"),
    ]
    assert len(places) == 12


def test_a_country_outside_the_table_is_offered_as_zone_tab_labels(zone_tab) -> None:
    assert region.place_zones("ES", zone_tab) == [
        ("Spain (mainland)", "Europe/Madrid"),
        ("Ceuta, Melilla", "Africa/Ceuta"),
        ("Canary Islands", "Atlantic/Canary"),
    ]


def test_a_zone_tab_line_without_a_comment_is_labelled_by_its_city(zone_tab) -> None:
    assert region.place_zones("FR", zone_tab) == [("Paris", "Europe/Paris")]


def test_a_zone_the_database_does_not_have_is_never_offered(zone_tab) -> None:
    assert region.place_zones("XX", zone_tab) == []


def test_without_zone_tab_only_the_table_answers(tmp_path) -> None:
    missing = str(tmp_path / "absent.tab")
    assert region.place_zones("SG", missing) == [("Singapore", "Asia/Singapore")]
    assert region.place_zones("ES", missing) == []


@given(code=st.sampled_from(["US", "CA", "ID", "MY", "SG", "ES", "FR", "PT", "IN", "XX", "ZZ"]))
def test_every_offered_place_is_a_zone_the_write_door_takes(code) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "zone.tab")
        with open(path, "w") as handle:
            handle.write(_ZONE_TAB)
        for _label, zone in region.place_zones(code, path):
            assert region.zone_error(zone) == "", zone


def test_a_city_is_matched_ignoring_case_spaces_and_underscores(zone_tab) -> None:
    assert region.zones_for_city("los angeles", zone_tab) == ["America/Los_Angeles"]
    assert region.zones_for_city("LOS_ANGELES", zone_tab) == ["America/Los_Angeles"]
    assert region.zones_for_city("  Madrid ", zone_tab) == ["Europe/Madrid"]


def test_a_city_with_no_zone_of_its_own_matches_nothing(zone_tab) -> None:
    assert region.zones_for_city("Barcelona", zone_tab) == []
    assert region.zones_for_city("", zone_tab) == []


def test_a_city_named_by_a_legacy_link_too_prefers_the_zone_tab_zone(zone_tab) -> None:
    # America/Indianapolis is a legacy link to the zone.tab name; one city is one answer, not two.
    assert region.zones_for_city("indianapolis", zone_tab) == ["America/Indiana/Indianapolis"]


@settings(max_examples=60)
@given(
    zone=st.sampled_from(
        ["America/Los_Angeles", "America/New_York", "Europe/Madrid", "Asia/Kolkata"]
    ),
    upper=st.booleans(),
    spaced=st.booleans(),
)
def test_a_zones_own_city_always_finds_it(zone, upper, spaced) -> None:
    city = zone.rsplit("/", 1)[-1]
    typed = city.replace("_", " ") if spaced else city
    typed = typed.upper() if upper else typed.lower()
    found = region.zones_for_city(typed, "/nonexistent/zone.tab")
    assert zone in found
    assert all(region.zone_error(name) == "" for name in found)
