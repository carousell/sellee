"""Who may drive a browser for a publish, and who may not.

A marketplace the agent can publish to deterministically has a driver precisely so that no model
does that work: `crosslist.enqueue_next` routes such a market to `_drive_publish` and never spawns
a pass. The seller-facing retry door did not ask that question, so the one market with a driver was
also the one market whose model pass got the full browser diet — free navigation, `browser_tabs`,
main-world `browser_evaluate` and `browser_handle_dialog` — with no recipe telling it what to do.

Two rules are pinned here, and they are separate on purpose:

* a driven market never runs as a model publish pass, asked where a pass is claimed so that work
  queued *before* the rule existed is refused too;
* browser authority is never granted without a recipe, so a grant can never outrun the
  instructions for using it.
"""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

import sellee.tools  # noqa: F401  registration
from sellee import marketplaces, passes
from sellee.browser import markets as market_adapters
from sellee.browser import publisher
from sellee.tools.registry import TIER_PASS_CHANNEL, dispatch

_RAIL_URL = "https://www.carousell.ai/listing/abc123"

# The one market with a deterministic driver, and one without — named rather than discovered so a
# registry change that moves a market between the two fails here loudly instead of silently
# retargeting every test in the file.
_DRIVEN = "fb"
_MODELLED = "carousell"


@pytest.fixture(autouse=True)
def _both_markets(store):
    # SG rather than US: it is the one region where both markets are publishable, which is what
    # lets the driven and the modelled case be compared under one seller.
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", [_DRIVEN, _MODELLED])


def test_the_named_markets_are_still_what_this_file_assumes(store) -> None:
    # Every assertion below reads from these facts; if the registry moves, say so here.
    assert publisher.can_drive(_DRIVEN)
    assert not publisher.can_drive(_MODELLED)
    assert not marketplaces.listing_flow(_DRIVEN)
    assert marketplaces.listing_flow(_MODELLED)
    assert set(market_adapters.publishable_markets("SG")) == {_DRIVEN, _MODELLED}


# --- a driven market never runs as a model pass -------------------------------------------------


def test_a_driven_market_is_refused_where_a_pass_is_claimed(store) -> None:
    # validate_payload is what the runner asks at claim time, so a pass queued before this rule
    # existed is refused on the way in rather than spawning against Facebook.
    with pytest.raises(passes.PassPayloadError) as caught:
        passes.validate_payload("publish", {"item_id": "i1", "market": _DRIVEN}, store)
    assert _DRIVEN in str(caught.value)


def test_a_modelled_market_still_validates(store) -> None:
    passes.validate_payload("publish", {"item_id": "i1", "market": _MODELLED}, store)


# --- browser authority follows the recipe -------------------------------------------------------


def test_a_driven_market_is_handed_no_browser_tools(store) -> None:
    assert passes._publish_browser_tools({"market": _DRIVEN}, store, "p1") == ()


def test_a_modelled_browser_market_keeps_its_diet(store) -> None:
    granted = passes._publish_browser_tools({"market": _MODELLED}, store, "p1")
    assert granted == passes.PUBLISH_BROWSER_TOOLS


def test_the_rail_is_handed_no_browser_tools(store) -> None:
    # A rail publish talks to an API; browser authority follows the market, not the pass type.
    assert passes._publish_browser_tools({}, store, "p1") == ()


# --- the seller-facing door ---------------------------------------------------------------------


@pytest.fixture
def item_on_the_rail(store):
    store.record_survey_result(_DRIVEN, [])
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell-ai", _RAIL_URL)
    return store.get_item(item["id"])


def test_the_retry_door_does_not_queue_a_model_pass_for_a_driven_market(
    make_ctx, store, item_on_the_rail
) -> None:
    ctx = make_ctx(TIER_PASS_CHANNEL, pass_id="p1", browser_factory=lambda: object())

    result = dispatch(
        "queue_marketplace_publish",
        {"item_id": item_on_the_rail["id"], "market": _DRIVEN},
        ctx,
    )

    assert result["status"] == "driven"
    assert result["market"] == _DRIVEN
    # Nothing queued: a pass here would be claimed, refused by validate_payload, and spend one of
    # the pair's three attempts on a refusal rather than on an attempt.
    assert [row for row in store.publish_pass_index() if row["status"] == "queued"] == []
