"""carousell_ai_publish_listing — compose a live listing over the wrapped rail, atomically.

The LLM never composes a price in cents or a listing URL. This tool: loads the item, converts
money in code, calls the rail's create_listing, verifies the returned URL live (fail-closed), and
only then records it into listing_urls in a store transaction. It is idempotent — an item already
carrying a carousell-ai URL returns that URL and never double-posts. The rail call runs outside
any store transaction, so the DB lock is never held across network I/O.
"""

from __future__ import annotations

from ..engines import pacing as pacing_engine
from ..money import to_price_cents
from ..rail.client import RailUnprovisioned
from ..store import StoreError
from .registry import (
    TIER_ATTENDED,
    TIER_PASS_PUBLISH,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_MARKET = "carousell-ai"


def _publish(ctx: ToolContext, params: dict) -> dict:
    item_id = params["item_id"]
    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")

    existing = item["listing_urls"].get(_MARKET)
    if existing:
        return {"listing_id": None, "url": existing, "already_published": True}

    if item.get("list_price") is None:
        raise ToolError("item has no list price — set one before publishing")
    if not (item.get("currency") or "").strip():
        raise ToolError("item has no currency — set one before publishing")
    try:
        price_cents = to_price_cents(item["list_price"])
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    # Every outbound marketplace action reserves through the pacing gate first — a publish is a
    # real action on the carousell-ai account, jitter-free (a slow human-paced form), but it still
    # counts against the per-marketplace hourly cap and quiet hours.
    cfg = pacing_engine.resolve(ctx.config)
    paced = ctx.store.reserve_action(
        marketplace=_MARKET,
        kind="publish",
        cfg=cfg,
        interactive=ctx.session.tier == TIER_ATTENDED,
    )
    if paced["verdict"] != "go":
        raise ToolError(
            f"paced: {paced['verdict']} on {_MARKET} "
            f"({paced['count']}/{paced['cap']} this hour) — retry in "
            f"{int(paced['delay_sec'])}s"
        )

    args = {
        "title": item["title"],
        "description": item["description"] or "",
        "price_cents": price_cents,
        "currency": item["currency"],
    }

    if ctx.rail_factory is None:
        raise ToolError("the carousell.ai rail is not available in this session")
    try:
        rail = ctx.rail_factory()
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `selly-agent provision carousell-ai`"
        ) from exc

    try:
        listing = rail.create_listing(args)
        rail.verify_listing_url(listing["url"])  # fail-closed: raises if not live under /listing/
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `selly-agent provision carousell-ai`"
        ) from exc
    except Exception as exc:  # RailError subclasses carry caller-safe, secret-free messages
        raise ToolError(str(exc)) from exc

    try:
        ctx.store.record_listing_url(item_id, _MARKET, listing["url"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    return {"listing_id": listing.get("listing_id"), "url": listing["url"]}


register(
    ToolSpec(
        name="carousell_ai_publish_listing",
        description="Publish an item as a live carousell.ai listing and record its verified URL. "
        "Idempotent: an already-published item returns its existing URL.",
        input_schema={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
        handler=_publish,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_PUBLISH}),
    )
)
