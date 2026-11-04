"""Thread tools: the identity-complete creator, reads, the field-constrained updater (with
listing_url verify-gated), and hold/release.

create_thread is new in the rewrite: legacy threads were born as fail-open skeletons or hand-seeded,
which is exactly what let identity-less threads silently disable own-echo / terminal suppression.
Here identity is required at creation, so that class of bug cannot exist. update_thread is
field-constrained in code — transcript/cursor advance, held, and sale-state transitions are owned by
their own tools, never this generic writer.
"""

from __future__ import annotations

from sellee import marketplaces
from sellee.rail.client import RailError, RailUnprovisioned
from sellee.store import IMPORTED_THREAD_SOURCES, StoreError
from sellee.tools import escalations
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)
from sellee.tools.verify import verify_market_url

# `scam` is not here: a scam report goes only through the seller's scam confirmation.
_BLOCK_REASONS = ("spam", "abuse", "other")
# A failed call can still have landed (a timeout after bazaar committed), so claim no outcome.
_BLOCK_NOT_CONFIRMED = (
    "carousell.ai did not confirm the block; nothing changed here, and asking again is safe "
    "even if the first one landed"
)


def _carousell_ai_block_buyer(ctx: ToolContext, params: dict) -> dict:
    reason = params["reason"]
    if reason not in _BLOCK_REASONS:
        raise ToolError(
            f"reason must be one of {', '.join(_BLOCK_REASONS)}; a scam is reported only through "
            "the seller's answer to the scam confirmation"
        )
    thread_id = params["thread_id"]
    thread = ctx.store.get_thread(thread_id, message_cap=0)
    if thread is None:
        raise ToolError(f"no thread with id {thread_id!r}")
    if thread["market"] != marketplaces.RAIL:
        raise ToolError("only a carousell.ai buyer can be blocked here")
    try:
        if ctx.rail_factory is None:
            raise RailUnprovisioned("no carousell.ai connection")
        ctx.rail_factory().block_buyer(thread_id.split(":", 1)[1], reason)
    except RailError:
        return {"thread_id": thread_id, "status": "block_failed", "detail": _BLOCK_NOT_CONFIRMED}
    # A held or escalated thread closes once released, as the relay lane does for any block.
    ctx.store.close_blocked_relay_thread(thread_id)
    return {"thread_id": thread_id, "status": "blocked", "reason": reason}


def _create_thread(ctx: ToolContext, params: dict) -> dict:
    if params.get("source") in IMPORTED_THREAD_SOURCES:
        raise ToolError(f"source {params['source']!r} is only for threads an inbox imports")
    listing_url = params.get("listing_url")
    if listing_url:
        result = verify_market_url(ctx, params["market"], listing_url, params.get("region"))
        if not result["ok"]:
            raise ToolError(f"listing_url failed verification: {result.get('reason')}")
    try:
        return ctx.store.create_thread(
            thread_id=params["thread_id"],
            side=params["side"],
            market=params["market"],
            counterpart_handle=params["counterpart_handle"],
            item_id=params.get("item_id"),
            want_id=params.get("want_id"),
            listing_url=listing_url,
            listed_price=params.get("listed_price"),
            buyer_location=params.get("buyer_location"),
            source=params.get("source"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _get_thread(ctx: ToolContext, params: dict) -> dict:
    thread = ctx.store.get_thread(params["thread_id"])
    if thread is None:
        raise ToolError(f"no thread with id {params['thread_id']!r}")
    return thread


def _list_threads(ctx: ToolContext, params: dict) -> dict:
    return {"threads": ctx.store.list_threads(side=params.get("side"), status=params.get("status"))}


def _update_thread(ctx: ToolContext, params: dict) -> dict:
    fields = dict(params["fields"])
    if fields.get("listing_url"):
        thread = ctx.store.get_thread(params["thread_id"])
        if thread is None:
            raise ToolError(f"no thread with id {params['thread_id']!r}")
        result = verify_market_url(ctx, thread["market"], fields["listing_url"])
        if not result["ok"]:
            raise ToolError(f"listing_url failed verification: {result.get('reason')}")
    try:
        return ctx.store.update_thread(params["thread_id"], fields)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _hold_thread(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.hold_thread(
            params["thread_id"], params["reason"], params.get("mark_handled_msg")
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _release_thread(ctx: ToolContext, params: dict) -> dict:
    escalations.refuse_answering_a_scam_confirmation(ctx.store, params["thread_id"])
    try:
        return ctx.store.release_thread(params["thread_id"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


register(
    ToolSpec(
        name="create_thread",
        description="Create an identity-complete marketplace thread. thread_id is the natural key "
        "'<market>:<local id>'; a sell thread requires item_id, a buy thread requires want_id.",
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "side": {"type": "string", "enum": ["sell", "buy"]},
                "market": {"type": "string"},
                "counterpart_handle": {"type": "string"},
                "item_id": {"type": "string"},
                "want_id": {"type": "string"},
                "listing_url": {"type": "string"},
                "listed_price": {"type": "number"},
                "buyer_location": {"type": "string"},
                "region": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["thread_id", "side", "market", "counterpart_handle"],
            "additionalProperties": False,
        },
        handler=_create_thread,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="get_thread",
        description="Fetch a thread record with its (capped) transcript. Never returns a floor or "
        "budget.",
        input_schema={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
        handler=_get_thread,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="list_threads",
        description="List thread summaries, optionally filtered by side and/or status.",
        input_schema={
            "type": "object",
            "properties": {
                "side": {"type": "string", "enum": ["sell", "buy"]},
                "status": {"type": "string"},
            },
            "additionalProperties": False,
        },
        handler=_list_threads,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="update_thread",
        description="Update writable thread fields (buyer_location, agent_note, listed_price, "
        "listing_url [verify-gated]); the only status flips here are escalated->active and "
        "active->closed.",
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "fields": {"type": "object", "additionalProperties": True},
            },
            "required": ["thread_id", "fields"],
            "additionalProperties": False,
        },
        handler=_update_thread,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="hold_thread",
        description="Hold a thread (stop engaging) without deleting it; the prior status is "
        "preserved for release. mark_handled_msg advances the cursor (the hold is the handling).",
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "reason": {"type": "string"},
                "mark_handled_msg": {"type": "string"},
            },
            "required": ["thread_id", "reason"],
            "additionalProperties": False,
        },
        handler=_hold_thread,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="release_thread",
        description="Release a held thread, restoring the status it had before the hold.",
        input_schema={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
        handler=_release_thread,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="carousell_ai_block_buyer",
        description="Block a carousell.ai buyer and report them to carousell.ai for spam, abuse "
        "or other. Use it only when the seller explicitly asks you to block this buyer, with the "
        "reason they gave. A scam is never reported here: that goes through the seller's answer "
        "to the scam confirmation. blocked means carousell.ai drops the buyer's further mail to "
        "this seller; block_failed means carousell.ai did not confirm, and asking again is safe.",
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "reason": {"type": "string", "enum": list(_BLOCK_REASONS)},
            },
            "required": ["thread_id", "reason"],
            "additionalProperties": False,
        },
        handler=_carousell_ai_block_buyer,
        # Only an attended session, where the seller is present to ask for it.
        tiers=frozenset({TIER_ATTENDED}),
    )
)
