"""Sell-side negotiation tools. The engine decides; the store runs load→decide→write in one
transaction (the FCFS single-inventory guarantee). No floor value ever crosses this boundary — a
needs_floor result carries no number, and the below-floor assert in the engine is the backstop.
"""

from __future__ import annotations

from sellee import settings
from sellee.store import StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)


def _require_pair(ctx: ToolContext, params: dict) -> None:
    """Refuse an (item_id, thread_id) pair that is not actually a pair.

    Both ids come from the model, and nothing downstream re-derives one from the other, so a
    mismatch files a real buyer's offer under the wrong item — silently, and with two expensive
    consequences. The correct item's `other_best` loses that buyer, so the engine may then accept
    BELOW a live rival's standing offer, which is the one guarantee the negotiation engine makes.
    And `negotiate_confirm_sold` builds its take-down list from the named item's listing urls, so a
    mismatched thread delists things that are still for sale.

    Checked here rather than in the store because this is the boundary the untrusted ids cross:
    every other caller reads both ids off the same thread row and cannot disagree with itself.
    """
    thread = ctx.store.get_thread(params["thread_id"])
    if thread is None:
        raise ToolError(f"no thread with id {params['thread_id']!r}")
    if thread["item_id"] != params["item_id"]:
        raise ToolError(
            f"thread {params['thread_id']!r} is about item {thread['item_id']!r}, not "
            f"{params['item_id']!r} — a negotiation must name the thread's own item"
        )


def _offer(ctx: ToolContext, params: dict) -> dict:
    if params["offer"] <= 0:
        raise ToolError("offer must be positive")
    _require_pair(ctx, params)
    try:
        return ctx.store.negotiate_offer(
            params["item_id"],
            params["thread_id"],
            params.get("buyer", ""),
            params["offer"],
            config=ctx.config,
            # read at the decision point, never cached — a firmness change applies to the very
            # next offer, with no daemon reload
            firmness=settings.get(ctx.store, "firmness"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _status(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.negotiate_status(params["item_id"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _confirm_bid(ctx: ToolContext, params: dict) -> dict:
    _require_pair(ctx, params)
    try:
        return ctx.store.negotiate_confirm_bid(params["item_id"], params["thread_id"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _confirm_sold(ctx: ToolContext, params: dict) -> dict:
    _require_pair(ctx, params)
    try:
        return ctx.store.negotiate_confirm_sold(params["item_id"], params["thread_id"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _release(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.negotiate_release(params["item_id"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


_ITEM_THREAD_SCHEMA = {
    "type": "object",
    "properties": {"item_id": {"type": "string"}, "thread_id": {"type": "string"}},
    "required": ["item_id", "thread_id"],
    "additionalProperties": False,
}
_ITEM_ONLY_SCHEMA = {
    "type": "object",
    "properties": {"item_id": {"type": "string"}},
    "required": ["item_id"],
    "additionalProperties": False,
}

register(
    ToolSpec(
        name="negotiate_offer",
        description="Decide the response to a buyer's offer on an item (counter / accept / hold / "
        "bid / needs_floor). Above-list bids never auto-accept — they require seller confirmation.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "buyer": {"type": "string"},
                "offer": {"type": "number"},
            },
            "required": ["item_id", "thread_id", "offer"],
            "additionalProperties": False,
        },
        handler=_offer,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="negotiate_status",
        description="Report an item's standing-offer / FCFS / bidding state.",
        input_schema=_ITEM_ONLY_SCHEMA,
        handler=_status,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="negotiate_confirm_bid",
        description="Reserve an item for the current leading bid (seller has approved it).",
        input_schema=_ITEM_THREAD_SCHEMA,
        handler=_confirm_bid,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="negotiate_confirm_sold",
        description="Mark an item sold to a thread; returns the other-market take-down list "
        "and the threads to close.",
        input_schema=_ITEM_THREAD_SCHEMA,
        handler=_confirm_sold,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="negotiate_release",
        description="Return an item to available after a sale fell through.",
        input_schema=_ITEM_ONLY_SCHEMA,
        handler=_release,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
