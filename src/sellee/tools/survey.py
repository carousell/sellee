"""The listings the seller already had, for the conversation to act on.

The ask itself is two buttons and needs no model — but "only the bike and the camera", "just answer
buyers, don't repost them" and "try that carousell.ai one again" are answers a button cannot carry.
These two tools honour them, over exactly the same rows the buttons write.

Neither tool adopts anything itself. They record a decision; the survey lane does the work and
reports it.
"""

from __future__ import annotations

from sellee import marketplaces
from sellee.store import StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

# What the seller may say about a set of listings. `retry` re-arms a carousell.ai publish that ran
# out of attempts — the door the failure notice promises.
_DECISIONS = ("manage", "decline", "retry")
_MANAGE_MODES = ("inbox", "relist")


def _list_discovered(ctx: ToolContext, params: dict) -> dict:
    market = params.get("market")
    rows = ctx.store.list_discovered_listings(market=market, status=params.get("status"))
    return {
        "listings": [
            {
                "market": row["market"],
                "listing_id": row["listing_id"],
                "title": row["title"],
                "price": row["price"],
                "price_text": row["price_text"],
                "url": row["url"],
                "status": row["status"],
                "manage": row["manage"],
                "item_id": row["item_id"],
                "rail_state": row["rail_state"],
            }
            for row in rows
        ]
    }


def _decide_discovered(ctx: ToolContext, params: dict) -> dict:
    market = params["market"]
    decision = params["decision"]
    if decision not in _DECISIONS:
        raise ToolError(f"decision must be one of {', '.join(_DECISIONS)}")
    if marketplaces.get_marketplace(market) is None:
        raise ToolError(f"no marketplace {market!r}")
    manage = params.get("manage") or ("relist" if decision == "manage" else None)
    if decision == "manage" and manage not in _MANAGE_MODES:
        raise ToolError(f"manage must be one of {', '.join(_MANAGE_MODES)}")
    try:
        moved = ctx.store.decide_discovered_listings(
            market,
            decision=decision,
            manage=manage,
            listing_ids=params.get("listing_ids"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    # Zero is an answer, not an error: the named listings were already decided, adopted or expired.
    return {"decided": moved, "market": market, "decision": decision, "manage": manage}


register(
    ToolSpec(
        name="list_discovered_listings",
        description="The listings the seller already had on a marketplace, which I found after "
        "they signed in — with what they said about each one. Use it when they answer the "
        "take-these-over question with anything other than a plain yes or no, so you can name the "
        "ones they mean. 'pending' means still waiting on their answer.",
        input_schema={
            "type": "object",
            "properties": {
                "market": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "accepted", "declined", "expired", "adopted", "failed"],
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_list_discovered,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
register(
    ToolSpec(
        name="decide_discovered_listings",
        description="Record what the seller wants done with listings they already had on a "
        "marketplace. 'manage' takes them over — 'relist' also puts them on carousell.ai, 'inbox' "
        "only answers buyers on them where they are. 'decline' leaves them alone. 'retry' has "
        "another go at a carousell.ai listing that failed. Omit listing_ids to mean everything "
        "still waiting. The work happens in the background and reports itself, so tell them it has "
        "started — never that a listing is up.",
        input_schema={
            "type": "object",
            "properties": {
                "market": {"type": "string"},
                "decision": {"type": "string", "enum": list(_DECISIONS)},
                "manage": {"type": "string", "enum": list(_MANAGE_MODES)},
                "listing_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["market", "decision"],
            "additionalProperties": False,
        },
        handler=_decide_discovered,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
