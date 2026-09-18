"""The agent never names the countries carousell.ai serves.

Which countries carousell.ai can take payments in is bazaar's answer, and it changes when a
Stripe platform account appears rather than when the agent ships. A copy of the list held here
is wrong the moment that happens, and it was wrong in both directions at once: it offered US,
which the backend did not serve, while refusing a seller in Vietnam, whom everything except
payouts works for. So the agent asks for a country, sends it, and relays what bazaar says.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "sellee"
REGISTRY = SRC / "data" / "marketplaces.json"

# The rail is one global site. Any other key here would be this list coming back as data.
RAIL = "carousell-ai"


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_module_derives_a_set_of_served_countries() -> None:
    # `supported_regions` was the function that did this, reading the rail's regional-site map as
    # if it said where a seller may live. Named explicitly so the idea cannot come back by name.
    for path in _sources():
        source = path.read_text()
        assert "supported_regions" not in source, path
        assert "supported_region" not in source, path


def test_no_string_in_the_agent_lists_the_countries_carousell_ai_serves() -> None:
    """The refusal this replaces read "XX isn't a country sellee works in yet — currently SG, US".
    A string like it anywhere in the agent is the old rule surviving as copy."""
    banned = ("works in yet", "countries we serve", "not a country sellee", "currently SG")
    for path in _sources():
        source = path.read_text()
        for phrase in banned:
            assert phrase not in source, f"{path}: {phrase!r}"


def test_the_rail_has_one_site_rather_than_a_country_map() -> None:
    entry = next(
        market
        for market in json.loads(REGISTRY.read_text())["marketplaces"]
        if market["id"] == RAIL
    )
    assert list(entry["domains"]) == ["*"], entry["domains"]


def test_the_write_door_takes_every_well_formed_country() -> None:
    """validate_basics is the one authority both writers go through, so a membership test
    reintroduced there would close the product to a country again. Swept rather than sampled:
    sampling inside the old served set is how the previous version of this looked fine."""
    from sellee.tools.seller import BasicsError, validate_basics

    alphabet = [chr(code) for code in range(ord("A"), ord("Z") + 1)]
    for first in alphabet:
        for second in alphabet:
            code = first + second
            assert validate_basics({"region": code})["region"] == code
    for malformed in ("V", "VNM", "V1", "1V", " V", ""):
        try:
            validate_basics({"region": malformed})
        except BasicsError:
            continue
        raise AssertionError(f"{malformed!r} was accepted as a country")
