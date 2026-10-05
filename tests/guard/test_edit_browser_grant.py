"""No marketplace is handed browser authority for an edit without a recipe for using it.

The same gap `test_publish_browser_grant.py` closes for publishing, opened again by a second pass
type: an edit pass gets the whole browser diet — free navigation of a logged-in account,
`browser_tabs`, main-world `browser_evaluate`, `browser_handle_dialog` — and what tells the model
what to *do* with it is a separate lookup (`marketplaces.edit_flow`). Reflecting over the registry
rather than naming markets, because the gap appears when a marketplace is *added*.
"""

from __future__ import annotations

import pytest

from sellee import marketplaces, passes
from sellee.browser import editor

_FIELD_SETS = (("list_price",), ("description",), ("photos",), ("title", "list_price"))


@pytest.mark.parametrize("changed", _FIELD_SETS)
def test_an_edit_grant_always_comes_with_a_recipe(changed) -> None:
    offenders = {}
    for entry in marketplaces.all_marketplaces():
        market = entry["id"]
        payload = {"market": market, "changed": list(changed)}
        if passes._edit_browser_tools(payload, None, "p1") and not marketplaces.edit_flow(market):
            offenders[market] = "browser tools granted with no edit_flow recipe"
    assert offenders == {}


@pytest.mark.parametrize("changed", _FIELD_SETS)
def test_an_edit_the_driver_can_make_is_never_a_model_pass(changed) -> None:
    offenders = {
        entry["id"]
        for entry in marketplaces.all_marketplaces()
        if editor.can_edit_fields(entry["id"], changed)
        and passes._edit_browser_tools(
            {"market": entry["id"], "changed": list(changed)}, None, "p1"
        )
    }
    assert offenders == set()


def test_the_rail_is_never_edited_in_a_browser() -> None:
    payload = {"market": marketplaces.RAIL, "changed": ["list_price"]}
    assert passes._edit_browser_tools(payload, None, "p1") == ()


def test_every_edit_recipe_named_in_the_registry_exists() -> None:
    from sellee import skills

    named = {marketplaces.edit_flow(e["id"]) for e in marketplaces.all_marketplaces()} - {""}
    assert named <= set(skills.available())


def test_every_field_an_adapter_offers_to_edit_is_one_the_driver_can_type() -> None:
    """An adapter naming an editable field the driver has no step for is a capability that says
    yes while the code says no. `can_edit_fields` already refuses it; this keeps the two lists from
    drifting apart silently. A market with its own edit driver answers for its fields itself."""
    from sellee.browser import markets as market_adapters

    offenders = {
        adapter.market: sorted(set(adapter.editable_fields) - set(editor._STEPS))
        for adapter in market_adapters.adapters()
        if not adapter.edit_driver and set(adapter.editable_fields) - set(editor._STEPS)
    }
    assert offenders == {}
