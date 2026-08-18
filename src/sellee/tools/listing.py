"""carousell_ai_update_listing — taking a rail listing down once the item is gone.

An item sells in one place, so every other listing for it has to come down. `negotiate_confirm_sold`
returns that list; this executes the rail half of it in one call — flip the listing's status, then
drop the URL from the item's record so nothing downstream still treats it as live.

The status used is `archived`, not `sold`. The item did sell, but not *here* — it went on another
marketplace, and telling the rail it sold there would report a sale that never happened on it.

Browser marketplaces are not taken down automatically. That would mean driving a rarely-exercised
recipe on the one path where a mistake is public and irreversible, so instead the seller gets a
needs-me item naming the listing to close by hand.
"""

from __future__ import annotations

from sellee import marketplaces
from sellee.rail.client import RailError, RailUnprovisioned, listing_id_from_url
from sellee.store import StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_MARKET = "carousell-ai"
_ARCHIVED = "archived"
_ACTIONS = ("take_down",)

MANUAL_TAKE_DOWN_NOTICE = (
    "One thing left on the sold item: its {market} listing is still up. "
    "Close it in the app when you get a chance — {url}"
)


def _listing_id(item: dict) -> str:
    return listing_id_from_url((item.get("listing_urls") or {}).get(_MARKET))


def _update_listing(ctx: ToolContext, params: dict) -> dict:
    item_id = params["item_id"]
    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")

    listing_id = _listing_id(item)
    if not listing_id:
        # Nothing to take down is a success, not a failure: the item was never listed on the rail.
        return {"status": "not_listed", "item_id": item_id}

    if ctx.rail_factory is None:
        raise ToolError("the carousell.ai rail is not available in this session")
    try:
        rail = ctx.rail_factory()
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `sellee provision carousell-ai`"
        ) from exc

    try:
        rail.update_listing(listing_id, status=_ARCHIVED)
    except RailError as exc:
        raise ToolError(str(exc)) from exc

    try:
        # Only after the rail has accepted it: a local record saying the listing is gone while it is
        # still live would leave a buyer able to reach it with nothing watching the thread.
        ctx.store.archive_listing_url(item_id, _MARKET)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc

    manual = _manual_take_downs(ctx, item_id)
    return {"status": "taken_down", "item_id": item_id, "manual_take_downs": manual}


def _manual_take_downs(ctx: ToolContext, item_id: str) -> list:
    """Queue a needs-me item per browser-market listing still up, and report them.

    Named rather than silently skipped: the whole point of confirming a sale is that the other
    listings come down, so the ones the agent will not close itself have to be visible work.
    """
    item = ctx.store.get_item(item_id)
    remaining = []
    for market, url in sorted((item or {}).get("listing_urls", {}).items()):
        if not url or marketplaces.connector_type(market) != "browser":
            continue
        ctx.store.queue_notice(
            MANUAL_TAKE_DOWN_NOTICE.format(market=marketplaces.display_name(market), url=url),
            ref=item_id,
        )
        remaining.append({"market": market, "url": url})
    return remaining


register(
    ToolSpec(
        name="carousell_ai_update_listing",
        description="Take an item's carousell.ai listing down once it has sold elsewhere (status "
        "archived + the URL dropped from the item). Any listing on a browser marketplace is "
        "reported back and queued for the seller to close by hand.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "action": {"type": "string", "enum": list(_ACTIONS)},
            },
            "required": ["item_id", "action"],
            "additionalProperties": False,
        },
        handler=_update_listing,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
