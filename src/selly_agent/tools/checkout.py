"""carousell_ai_create_checkout_link — the checkout-at-close, composed atomically.

Legacy's three-step choreography (precheck → LLM MCP call → record) collapses into one tool:
floor gate → sale-id idempotency → resolve listing_id → mint over the rail (outside any store
transaction) → fail-closed URL-base validation → record. The floor never crosses the boundary: a
below-floor close returns a structured below_floor error carrying no value, a floorless below-list
close returns no_floor (ask the seller), and an unpublished item returns not_published naming the
publish tool — the legacy self-heal (inline create-then-checkout) is deliberately not ported.
"""

from __future__ import annotations

import hashlib

from ..money import to_price_cents
from ..rail.client import RailError, RailUnprovisioned
from ..store import StoreError
from .registry import (
    TIER_ATTENDED,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_MARKET = "carousell-ai"


def _sale_id(item_id: str, thread_id: str, price) -> str:
    seed = f"{item_id}|{thread_id}|{price}".encode()
    return hashlib.sha256(seed).hexdigest()[:12]  # an id, not a security hash


def _listing_id(item: dict) -> str:
    url = str((item.get("listing_urls") or {}).get(_MARKET) or "")
    if "/listing/" not in url:
        return ""
    tail = url.split("/listing/", 1)[1]
    return tail.split("/", 1)[0].split("?", 1)[0].strip()


def _checkout_base(ctx: ToolContext) -> str:
    return ctx.config.carousell_ai_api_base.rstrip("/") + "/checkout"


def _create_checkout_link(ctx: ToolContext, params: dict) -> dict:
    item_id = params["item_id"]
    thread_id = params["thread_id"]
    price = params["agreed_price"]

    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")

    # floor gate first — the store returns only a status, never the floor value
    try:
        gate = ctx.store.checkout_floor_gate(item_id, price)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    if gate["status"] == "below_floor":
        raise ToolError("agreed price is not acceptable for this item")
    if gate["status"] == "no_floor":
        raise ToolError(
            "no floor is set and the price is below list — ask the seller for their floor first"
        )

    sale_id = _sale_id(item_id, thread_id, price)
    existing = ctx.store.get_checkout(sale_id)
    if existing:
        return {"checkout_url": existing["checkout_url"], "already_issued": True}

    listing_id = _listing_id(item)
    if not listing_id:
        raise ToolError(
            "item is not published on carousell.ai — publish it first with "
            "carousell_ai_publish_listing, then create the checkout link"
        )

    if ctx.rail_factory is None:
        raise ToolError("the carousell.ai rail is not available in this session")
    try:
        rail = ctx.rail_factory()
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `selly-agent provision carousell-ai`"
        ) from exc

    try:
        price_cents = to_price_cents(price)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    try:
        minted = rail.create_checkout({"listing_id": listing_id, "agreed_price_cents": price_cents})
    except RailError as exc:
        raise ToolError(str(exc)) from exc

    url = (minted.get("checkout_url") or "").strip()
    base = _checkout_base(ctx)
    if not url.startswith(base + "/"):
        # fail closed: a minted link must sit under the config-derived checkout base
        raise ToolError("checkout link did not come from the expected carousell.ai checkout base")

    recorded = ctx.store.record_checkout(
        sale_id=sale_id,
        item_id=item_id,
        thread_id=thread_id,
        checkout_url=url,
        price=price,
        currency=gate["currency"],
    )
    return {"checkout_url": recorded["checkout_url"]}


register(
    ToolSpec(
        name="carousell_ai_create_checkout_link",
        description="Mint (or return the already-issued) carousell.ai checkout link for a "
        "finalised deal. Floor-gated and idempotent; an unpublished item returns not_published.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "agreed_price": {"type": "number"},
            },
            "required": ["item_id", "thread_id", "agreed_price"],
            "additionalProperties": False,
        },
        handler=_create_checkout_link,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
