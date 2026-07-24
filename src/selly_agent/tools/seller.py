"""Seller-config tools and the shipping quote.

get_seller_config returns a buyer-safe view — every section except the exact origin address, which
is stored but never returned by any read. update_seller_config is the validated writer (origin is
a secret param, masked before any sink). quote_shipping is the only consumer of the zone table; it
composes the pure shipping engine with the item and the seller's zones, and the origin address
never enters the computation or the output.
"""

from __future__ import annotations

from selly_agent.engines import shipping as shipping_engine
from selly_agent.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_BASICS_KEYS = {"region", "currency", "timezone"}
_SHIPPING_KEYS = {"zones", "size_surcharge"}


def _get_seller_config(ctx: ToolContext, params: dict) -> dict:
    return {"seller_config": ctx.store.get_seller_config_public()}


def _update_seller_config(ctx: ToolContext, params: dict) -> dict:
    written = []
    if "basics" in params:
        basics = params["basics"]
        unknown = set(basics) - _BASICS_KEYS
        if unknown:
            raise ToolError(f"unknown basics key(s): {', '.join(sorted(unknown))}")
        ctx.store.set_seller_config_section("basics", basics)
        written.append("basics")
    if "shipping" in params:
        shipping = params["shipping"]
        unknown = set(shipping) - _SHIPPING_KEYS
        if unknown:
            raise ToolError(f"unknown shipping key(s): {', '.join(sorted(unknown))}")
        ctx.store.set_seller_config_section("shipping", shipping)
        written.append("shipping")
    if "origin" in params:
        # The exact street address — stored, never returned by any read tool.
        ctx.store.set_seller_config_section("origin", params["origin"])
        written.append("origin")
    if not written:
        raise ToolError("provide at least one of basics, shipping, origin")
    return {"status": "written", "sections": written}


def _quote_shipping(ctx: ToolContext, params: dict) -> dict:
    item = ctx.store.get_item(params["item_id"])
    if item is None:
        raise ToolError(f"no item with id {params['item_id']!r}")
    price = item.get("list_price")
    if not isinstance(price, (int, float)):
        raise ToolError("item has no numeric list price — set one before quoting shipping")
    shipping = ctx.store.get_seller_config_section("shipping") or {}
    zones = shipping.get("zones")
    if not isinstance(zones, list) or not zones:
        raise ToolError("shipping is not configured — set seller shipping zones first")
    size_surcharge = shipping.get("size_surcharge") or {}
    basics = ctx.store.get_seller_config_section("basics") or {}
    currency = basics.get("currency") or item.get("currency") or ""
    size_bucket = item.get("size_bucket") or "small"
    return shipping_engine.compute(
        zones,
        size_surcharge,
        price,
        size_bucket,
        currency,
        params.get("dest_area", ""),
        params.get("dest_km"),
    )


register(
    ToolSpec(
        name="get_seller_config",
        description="Return the buyer-safe seller configuration (basics, shipping zones, …). The "
        "exact origin address is never returned.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_get_seller_config,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="update_seller_config",
        description="Write seller configuration sections: basics (region/currency/timezone), "
        "shipping (zones/size_surcharge), and origin (the private pickup address).",
        input_schema={
            "type": "object",
            "properties": {
                "basics": {"type": "object", "additionalProperties": True},
                "shipping": {"type": "object", "additionalProperties": True},
                "origin": {"type": "object", "additionalProperties": True},
            },
            "additionalProperties": False,
        },
        handler=_update_seller_config,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
        secret_params=frozenset({"origin"}),
    )
)
register(
    ToolSpec(
        name="quote_shipping",
        description="Compute the buyer's total (price + delivery fee + size surcharge) for a "
        "destination from the seller's zone table. Unserviceable destinations are not covered.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "dest_area": {"type": "string"},
                "dest_km": {"type": "number"},
            },
            "required": ["item_id"],
            "additionalProperties": False,
        },
        handler=_quote_shipping,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
