"""The shipping engine + the seller-config tools + quote_shipping. The exact origin address is
stored but never returned by any read, and never enters the fee computation."""

from __future__ import annotations

import pytest

from sellee.engines import shipping
from sellee.tools.registry import ToolError, dispatch

_ZONES = [
    {"zone": "central", "match": {"areas": ["tampines", "bedok"]}, "fee": 5},
    {"zone": "far", "match": {"max_km": 30}, "fee": 12},
    {"zone": "nationwide", "match": {"areas": ["__else__"]}, "fee": 8},
]
_SURCHARGE = {"large": 10}


# --- engine ------------------------------------------------------------------------------------


def test_first_matching_zone_wins_in_seller_order() -> None:
    r = shipping.compute(_ZONES, _SURCHARGE, 80, "small", "SGD", "tampines", None)
    assert r["zone"] == "central" and r["delivery_fee"] == 5


def test_buyer_total_invariant() -> None:
    r = shipping.compute(_ZONES, _SURCHARGE, 80, "large", "SGD", "tampines", None)
    # buyer_total = price + delivery_fee + size_surcharge
    assert r["size_surcharge"] == 10
    assert r["total_fee"] == 15
    assert r["buyer_total"] == 95


def test_else_catch_all_and_distance_band() -> None:
    assert shipping.compute(_ZONES, _SURCHARGE, 80, "small", "SGD", "nowhere", None)["zone"] == (
        "nationwide"
    )
    assert shipping.compute(_ZONES, _SURCHARGE, 80, "small", "SGD", "", 20)["zone"] == "far"


def test_unserviceable_returns_covered_false() -> None:
    zones = [{"zone": "only", "match": {"areas": ["central"]}, "fee": 5}]
    r = shipping.compute(zones, {}, 80, "small", "SGD", "outer-island", None)
    assert r["covered"] is False
    assert r["delivery_fee"] is None and r["total_fee"] is None and r["buyer_total"] is None


def test_null_size_bucket_defaults_small_surcharge_zero() -> None:
    r = shipping.compute(_ZONES, _SURCHARGE, 80, "small", "SGD", "tampines", None)
    assert r["size_surcharge"] == 0  # no 'small' key → 0


# --- tools -------------------------------------------------------------------------------------


def test_seller_config_never_returns_origin(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    dispatch(
        "update_seller_config",
        {
            "basics": {"region": "SG", "currency": "SGD"},
            "shipping": {"zones": _ZONES, "size_surcharge": _SURCHARGE},
            "origin": {"address": "12 Secret Lane, #04-05"},
        },
        ctx,
    )
    view = dispatch("get_seller_config", {}, ctx)["seller_config"]
    assert "basics" in view and "shipping" in view
    assert "origin" not in view  # the exact address is never returned
    # and it truly is stored (write-only), reachable only by internal code
    assert store.get_seller_config_section("origin") == {"address": "12 Secret Lane, #04-05"}


def test_update_seller_config_rejects_unknown_keys(make_ctx) -> None:
    ctx = make_ctx("attended")
    with pytest.raises(ToolError, match="basics key"):
        dispatch("update_seller_config", {"basics": {"nope": 1}}, ctx)


def test_update_seller_config_requires_a_section(make_ctx) -> None:
    ctx = make_ctx("attended")
    with pytest.raises(ToolError, match="at least one"):
        dispatch("update_seller_config", {}, ctx)


def test_quote_shipping_composes_engine_and_never_emits_address(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.update_item(item["id"], {"size_bucket": "large"})
    dispatch(
        "update_seller_config",
        {
            "shipping": {"zones": _ZONES, "size_surcharge": _SURCHARGE},
            "origin": {"address": "12 Secret Lane"},
        },
        ctx,
    )
    quote = dispatch("quote_shipping", {"item_id": item["id"], "dest_area": "tampines"}, ctx)
    assert quote["covered"] is True and quote["buyer_total"] == 95
    assert "12 Secret Lane" not in str(quote) and "address" not in quote


def test_quote_shipping_requires_configured_zones(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    with pytest.raises(ToolError, match="shipping is not configured"):
        dispatch("quote_shipping", {"item_id": item["id"], "dest_area": "tampines"}, ctx)
