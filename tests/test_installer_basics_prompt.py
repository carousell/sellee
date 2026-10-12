"""The timezone question setup asks when the machine cannot vouch for it: the places it offers,
the cities it matches, and what it defaults to."""

from __future__ import annotations

import io

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sellee.installer import basics_prompt
from sellee.installer import region as region_guess
from sellee.installer.ui import Abort, Ui

ZONE_TAB = """\
ES\t+4024-00341\tEurope/Madrid\tSpain (mainland)
ES\t+3553-00519\tAfrica/Ceuta\tCeuta, Melilla
ES\t+2806-01524\tAtlantic/Canary\tCanary Islands
FR\t+4852+00220\tEurope/Paris
US\t+404251-0740023\tAmerica/New_York\tEastern (most areas)
US\t+394606-0860929\tAmerica/Indiana/Indianapolis\tEastern - IN (most areas)
US\t+415100-0873900\tAmerica/Chicago\tCentral (most areas)
US\t+394421-1045903\tAmerica/Denver\tMountain (most areas)
US\t+340308-1181434\tAmerica/Los_Angeles\tPacific
"""


@pytest.fixture
def zone_tab(monkeypatch, tmp_path):
    path = tmp_path / "zone.tab"
    path.write_text(ZONE_TAB)
    monkeypatch.setattr(region_guess, "zone_tab_path", lambda: str(path))
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "")
    return path


def _ask_timezone(region, replies):
    """Run the timezone question over a script; a script that runs out ends it as Ctrl-D would."""
    answers = iter(replies)

    def reply():
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    out = io.StringIO()
    ui = Ui(stream=out, interactive=True, color=False, input_fn=reply)
    return basics_prompt.ask_timezone(ui, region), out.getvalue()


def test_a_country_outside_the_table_is_offered_its_zone_tab_places(zone_tab) -> None:
    zone, out = _ask_timezone("ES", ["3"])
    assert zone == "Atlantic/Canary"
    assert "1) Spain (mainland) — Madrid\n2) Ceuta, Melilla\n3) Canary Islands\n" in out
    assert "4) Somewhere else — type a city" in out


def test_somewhere_else_takes_a_city_and_confirms_its_zone(zone_tab) -> None:
    zone, out = _ask_timezone("US", ["14", "los angeles", ""])
    assert zone == "America/Los_Angeles"
    assert "Which city are you in?" in out
    assert "America/Los_Angeles, correct? [Y/n]" in out


def test_a_city_confirmed_wrong_is_asked_again(zone_tab) -> None:
    zone, _ = _ask_timezone("US", ["14", "los angeles", "n", "denver", "y"])
    assert zone == "America/Denver"


def test_a_city_with_no_zone_is_pointed_at_one_of_the_countrys_own(zone_tab) -> None:
    zone, out = _ask_timezone("US", ["14", "Dallas", "Chicago", ""])
    assert zone == "America/Chicago"
    assert (
        "No timezone is named after 'Dallas' — try a city in your timezone, like New York." in out
    )


def test_a_city_with_no_zone_in_a_country_with_no_places_asks_for_a_big_city(
    zone_tab, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(region_guess, "zone_tab_path", lambda: str(tmp_path / "absent.tab"))
    zone, out = _ask_timezone("ES", ["Barcelona", "Asia/Singapore"])
    assert zone == "Asia/Singapore"
    assert "try the nearest big city" in out


def test_a_city_several_zones_share_is_picked_from_a_list(zone_tab, monkeypatch, tmp_path) -> None:
    import zoneinfo

    if not {"Australia/West", "Brazil/West"} <= zoneinfo.available_timezones():
        pytest.skip("this zone database has no legacy West links")
    monkeypatch.setattr(region_guess, "zone_tab_path", lambda: str(tmp_path / "absent.tab"))
    zone, out = _ask_timezone("BR", ["west", "2"])
    assert zone == "Brazil/West"
    assert "1) Australia/West\n2) Brazil/West\n" in out


def test_typing_utc_is_not_asked_to_choose_between_its_names(zone_tab) -> None:
    zone, out = _ask_timezone("ES", ["4", "utc", ""])
    assert zone == "Etc/UTC"
    assert "Which of these?" not in out


def test_without_zone_tab_the_table_still_lists_and_others_get_the_city_question(
    zone_tab, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(region_guess, "zone_tab_path", lambda: str(tmp_path / "absent.tab"))
    zone, out = _ask_timezone("US", ["2"])
    assert zone == "America/Chicago"
    zone, out = _ask_timezone("ES", ["madrid", ""])
    assert zone == "Europe/Madrid"
    assert out.startswith("Which city are you in?")


def test_a_one_zone_country_is_proposed_as_before(zone_tab) -> None:
    zone, out = _ask_timezone("FR", [""])
    assert zone == "Europe/Paris"
    assert "Timezone? [Europe/Paris]" in out


@pytest.mark.parametrize("enter", ["", "2"])
def test_a_one_zone_country_is_offered_beside_a_machine_zone_outside_it(
    zone_tab, monkeypatch, enter
) -> None:
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "UTC")
    zone, out = _ask_timezone("FR", [enter])
    assert zone == ("Europe/Paris" if enter else "UTC")
    assert "1) UTC (this computer)\n2) Paris\n3) Somewhere else — type a city\n" in out
    assert "[default 1]" in out


@pytest.mark.parametrize("region", ["US", "ES"])
def test_without_the_machines_zone_no_place_is_the_default(zone_tab, region) -> None:
    # Nothing makes the first place likelier than the rest, so Enter skips rather than picks it.
    zone, out = _ask_timezone(region, [""])
    assert zone == ""
    assert "[default" not in out
    assert "or empty to skip" in out
    assert "no timezone recorded — ask Sellee to set one any time" in out


def test_the_machines_zone_is_the_default_place(zone_tab, monkeypatch) -> None:
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "America/Denver")
    zone, out = _ask_timezone("US", [""])
    assert zone == "America/Denver"
    assert "[default 3]" in out


def test_a_zone_only_zone_tab_lists_follows_the_tables_own(zone_tab, monkeypatch) -> None:
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "America/Indiana/Indianapolis")
    zone, out = _ask_timezone("US", [""])
    assert zone == "America/Indiana/Indianapolis"
    assert "(this computer)" not in out
    assert "[default 13]" in out


@pytest.mark.parametrize("machine", ["UTC", "Asia/Singapore"])
def test_a_machine_zone_outside_the_country_stays_the_default(
    zone_tab, monkeypatch, machine
) -> None:
    monkeypatch.setattr(region_guess, "system_timezone", lambda: machine)
    zone, out = _ask_timezone("US", [""])
    assert zone == machine
    assert f"1) {region_guess.city_label(machine)} (this computer)\n2) Eastern — New York\n" in out


def test_a_zone_name_without_a_slash_is_taken_as_typed(zone_tab) -> None:
    zone, _ = _ask_timezone("ES", ["4", "UTC"])
    assert zone == "UTC"


def test_a_typed_zone_name_is_still_accepted(zone_tab) -> None:
    zone, _ = _ask_timezone("US", ["14", "Asia/Singapore"])
    assert zone == "Asia/Singapore"


def test_an_empty_city_skips_with_the_note(zone_tab) -> None:
    zone, out = _ask_timezone("ES", ["4", ""])
    assert zone == ""
    assert "no timezone recorded — ask Sellee to set one any time" in out


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    region=st.sampled_from(["US", "ES", "FR", "SG", "BR"]),
    replies=st.lists(
        st.sampled_from(
            [
                "",
                "1",
                "4",
                "13",
                "99",
                "y",
                "n",
                "los angeles",
                "Barcelona",
                "madrid",
                "indianapolis",
                "Asia/Singapore",
                "Asia/Singapur",
                "gmt8+",
                "UTC",
            ]
        ),
        max_size=8,
    ),
)
def test_whatever_is_typed_only_a_storable_zone_or_nothing_is_returned(
    zone_tab, region, replies
) -> None:
    try:
        zone, _ = _ask_timezone(region, replies)
    except Abort:
        return
    assert zone == "" or region_guess.zone_error(zone) == ""
