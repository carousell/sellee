"""Seller-config tools and the shipping quote.

get_seller_config returns a buyer-safe view — every section except the exact origin address, which
is stored but never returned by any read. update_seller_config is the validated writer (origin is
a secret param, masked before any sink). quote_shipping is the only consumer of the zone table; it
composes the pure shipping engine with the item and the seller's zones, and the origin address
never enters the computation or the output.
"""

from __future__ import annotations

from sellee import marketplaces
from sellee.engines import shipping as shipping_engine
from sellee.tools.registry import (
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


class BasicsError(ValueError):
    """A seller-basics value is malformed. The message is caller-facing."""


def validate_basics(basics: dict) -> dict:
    """Check and canonicalize {region, currency, timezone}, the way both its writers need it.

    Region and currency are upper-cased here because every downstream lookup is an exact match:
    the marketplace registry keys its regional sites by "SG", so a stored "sg" resolves to no
    site at all and reads as "no marketplace is available to you". The timezone is checked
    against the system's zone database when there is one — a typo there is a silently wrong
    quiet-hours window months later, not an error anyone would connect back to setup.
    """
    if not isinstance(basics, dict):
        raise BasicsError("basics must be an object with region, currency and timezone")
    unknown = set(basics) - _BASICS_KEYS
    if unknown:
        raise BasicsError(f"unknown basics key(s): {', '.join(sorted(unknown))}")

    out = dict(basics)
    region = str(basics.get("region", "")).strip()
    if "region" in basics:
        if len(region) != 2 or not region.isalpha():
            raise BasicsError(f"region must be a two-letter country code (e.g. SG), got {region!r}")
        supported = marketplaces.supported_regions()
        if region.upper() not in supported:
            # Refused rather than stored, because storing it produces a seller who looks
            # configured and is not: provisioning has no country to ask for, and every listing
            # goes on the rail, so nothing they list can go anywhere.
            raise BasicsError(
                f"{region.upper()} isn't a country sellee works in yet — "
                f"currently {', '.join(supported)}"
            )
        out["region"] = region.upper()

    currency = str(basics.get("currency", "")).strip()
    if "currency" in basics:
        if len(currency) != 3 or not currency.isalpha():
            raise BasicsError(f"currency must be a three-letter code (e.g. SGD), got {currency!r}")
        out["currency"] = currency.upper()

    timezone = str(basics.get("timezone", "")).strip()
    if "timezone" in basics:
        if not timezone:
            raise BasicsError("timezone must be a zone name (e.g. Asia/Singapore)")
        _require_known_timezone(timezone)
        out["timezone"] = timezone
    return out


def _require_known_timezone(name: str) -> None:
    """Refuse a timezone the zone database does not have — unless there is no database at all.

    "Not found" and "no database" look identical from one lookup, so a name known to exist is
    tried first: if even UTC cannot be resolved there is nothing to check against and the shape
    is all we can vouch for. A structurally invalid name (an absolute path, a `..` component)
    raises ValueError and is always refused — it is malformed, not merely unknown.
    """
    import zoneinfo

    try:
        zoneinfo.ZoneInfo(name)
        return
    except zoneinfo.ZoneInfoNotFoundError:
        pass
    except (ValueError, OSError) as exc:
        raise BasicsError(f"{name!r} is not a valid timezone name (e.g. Asia/Singapore)") from exc

    try:
        zoneinfo.ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 — no usable zone database; nothing to validate against
        return
    raise BasicsError(f"unknown timezone {name!r} (e.g. Asia/Singapore)")


def _get_seller_config(ctx: ToolContext, params: dict) -> dict:
    return {"seller_config": ctx.store.get_seller_config_public()}


def _update_seller_config(ctx: ToolContext, params: dict) -> dict:
    written = []
    if "basics" in params:
        try:
            basics = validate_basics(params["basics"])
        except BasicsError as exc:
            raise ToolError(str(exc)) from exc
        # Merged, the same way the installer's door writes it: validate_basics accepts partial
        # updates, and a currency tweak that silently dropped the stored region would leave a
        # seller nothing can publish for — with no error anywhere near the cause.
        current = ctx.store.get_seller_config_section("basics") or {}
        ctx.store.set_seller_config_section("basics", {**current, **basics})
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
