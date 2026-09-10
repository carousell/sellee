"""Craigslist's areas, and the two gates that decide which of them we serve.

Craigslist's unit of geography is one of 707 areas, and it carries two identifiers that are not
interchangeable: the `hostname` a seller sees in their own URL (`/area/sfbay`) and the
`abbreviation` the posting flow requires (`/c/sfo`). Feeding the hostname to the posting flow
returns HTTP 200 and lands on a generic "choose area" picker, so the wrong one fails silently —
which is why resolution always answers the abbreviation.
"""

from __future__ import annotations

import pytest

from sellee import craigslist_areas

# --- the snapshot ------------------------------------------------------------------------------


def test_the_snapshot_loaded() -> None:
    """Guards the guard: an empty snapshot would make every assertion below vacuous."""
    assert len(craigslist_areas.areas()) > 500


def test_abbreviations_are_unique() -> None:
    """The abbreviation is the key everything else resolves to, so a duplicate would make
    resolution ambiguous in a way no caller could detect."""
    abbreviations = [area["abbreviation"] for area in craigslist_areas.areas()]
    assert len(abbreviations) == len(set(abbreviations))


def test_the_snapshot_carries_the_state_for_us_areas() -> None:
    """Craigslist's own `Region` field is a US state code, and it is what the legal gate reads.
    Non-US areas carry none, which is why the state check is only meaningful for US areas."""
    assert craigslist_areas.get("sfo")["state"] == "CA"
    assert craigslist_areas.get("lax")["state"] == "CA"
    assert craigslist_areas.get("nyc")["state"] == "NY"
    assert craigslist_areas.get("sng")["state"] == ""
    assert craigslist_areas.get("sng")["country"] == "SG"


def test_the_bay_area_carries_its_subareas() -> None:
    """The listing kit names these so a seller picks the one nearest them: `sfo` spans the whole
    Bay Area, and a Berkeley seller who picks Santa Cruz has posted where their buyers are not."""
    subareas = craigslist_areas.subareas("sfo")
    assert [sub["abbreviation"] for sub in subareas] == ["sfc", "sby", "eby", "pen", "nby", "scz"]
    assert subareas[0]["description"] == "city of san francisco"


def test_an_area_with_no_subareas_answers_empty() -> None:
    assert craigslist_areas.subareas("sng") == []


# --- resolution: hostname, description or abbreviation in; abbreviation out --------------------


@pytest.mark.parametrize("said", ["sfo", "sfbay", "SF bay area", "  SFBAY  ", "sf bay area"])
def test_every_way_a_seller_might_name_the_bay_area_resolves_to_its_abbreviation(said) -> None:
    assert craigslist_areas.resolve(said) == "sfo"


def test_a_pasted_area_url_resolves() -> None:
    """The hostname is what a seller can actually see and copy, from `/area/<hostname>`."""
    assert craigslist_areas.resolve("https://www.craigslist.org/area/sfbay") == "sfo"
    assert craigslist_areas.resolve("sfbay.craigslist.org") == "sfo"


def test_resolution_never_answers_the_hostname() -> None:
    """The whole point: `post.craigslist.org/c/sfbay` answers 200 and drops the flow into a
    generic area picker, so a hostname must never reach the posting URL."""
    for said in ("sfo", "sfbay", "SF bay area"):
        assert craigslist_areas.resolve(said) != "sfbay"


def test_an_unknown_area_is_refused_by_name() -> None:
    with pytest.raises(craigslist_areas.UnknownArea) as caught:
        craigslist_areas.resolve("atlantis")
    assert "atlantis" in str(caught.value)


def test_a_subarea_name_resolves_to_its_parent_area() -> None:
    """The names people use are often subareas: Craigslist calls the launch area "SF bay area",
    while "city of san francisco" is a subarea of it."""
    assert craigslist_areas.resolve("city of san francisco") == "sfo"
    assert craigslist_areas.resolve("east bay area") == "sfo"


def test_an_unknown_area_names_close_matches() -> None:
    """A typo should not read as "craigslist does not go there"."""
    with pytest.raises(craigslist_areas.UnknownArea) as caught:
        craigslist_areas.resolve("sfbey")
    assert "sfo" in str(caught.value)


def test_a_place_name_craigslist_spells_differently_earns_a_suggestion() -> None:
    """Craigslist has no area called "san francisco" — its area is "SF bay area". Rather than guess
    through that, the refusal points at the right abbreviation."""
    with pytest.raises(craigslist_areas.UnknownArea) as caught:
        craigslist_areas.resolve("san francisco")
    assert "sfo" in str(caught.value)


# --- the two gates -----------------------------------------------------------------------------


def test_the_rollout_sits_inside_the_legal_envelope() -> None:
    """The assertion that makes adding a state *safe* rather than merely easy: an area whose state
    we are not cleared for cannot be launched, because this fails until the state is added too."""
    states = {craigslist_areas.get(area)["state"] for area in craigslist_areas.CRAIGSLIST_AREAS}
    assert states <= craigslist_areas.SERVED_STATES


def test_san_francisco_is_the_launch_area() -> None:
    assert craigslist_areas.CRAIGSLIST_AREAS == frozenset({"sfo"})
    assert craigslist_areas.SERVED_STATES == frozenset({"CA"})


def test_the_launch_area_is_servable() -> None:
    craigslist_areas.check_servable("sfo")


def test_an_area_inside_the_envelope_but_outside_the_rollout_is_not_yet() -> None:
    """Los Angeles is in California, so it is legal — we have simply not launched there. The copy
    must not tell that seller their account is broken."""
    with pytest.raises(craigslist_areas.AreaNotAvailable) as caught:
        craigslist_areas.check_servable("lax")
    assert caught.value.reason == "not_yet"
    # Reads as a rollout limit, and names where we do work — not as a broken account.
    assert "yet" in str(caught.value).lower()
    assert "SF bay area" in str(caught.value)


def test_an_area_outside_the_envelope_is_not_served() -> None:
    with pytest.raises(craigslist_areas.AreaNotAvailable) as caught:
        craigslist_areas.check_servable("nyc")
    assert caught.value.reason == "not_served"


def test_the_two_refusals_do_not_read_alike() -> None:
    """A waitlist and a jurisdiction we cannot serve are different answers, and a seller acts on
    them differently."""
    with pytest.raises(craigslist_areas.AreaNotAvailable) as not_yet:
        craigslist_areas.check_servable("lax")
    with pytest.raises(craigslist_areas.AreaNotAvailable) as not_served:
        craigslist_areas.check_servable("nyc")
    assert str(not_yet.value) != str(not_served.value)


def test_a_non_us_area_is_not_served() -> None:
    """Craigslist runs a Singapore site; we do not serve it, and it has no state to compare."""
    with pytest.raises(craigslist_areas.AreaNotAvailable) as caught:
        craigslist_areas.check_servable("sng")
    assert caught.value.reason == "not_served"


def test_an_unknown_area_is_not_a_gate_answer() -> None:
    """`check_servable` speaks about areas that exist; an unknown one is a resolution failure and
    must not be reported as a jurisdiction we do not serve."""
    with pytest.raises(craigslist_areas.UnknownArea):
        craigslist_areas.check_servable("atlantis")


# --- what the country check needs --------------------------------------------------------------


def test_an_area_knows_which_country_it_is_in() -> None:
    """The seller-facing check refuses an area in a different country from the seller's region, so
    a US seller can never be pointed at `singapore`."""
    assert craigslist_areas.get("sfo")["country"] == "US"
    assert craigslist_areas.get("sng")["country"] == "SG"


def test_get_is_none_for_an_unknown_area() -> None:
    assert craigslist_areas.get("atlantis") is None


def test_the_display_name_reads_as_a_place_not_a_code() -> None:
    """Rendered back to the seller for confirmation — a bare `sfo` is not an answer to "where"."""
    assert craigslist_areas.display_name("sfo") == "SF bay area (sfbay)"
