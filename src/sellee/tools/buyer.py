"""Buy-side tools: the hardened set_budget writer and the buyer_negotiate_* verbs.

set_budget mirrors set_floor's discipline (validation, provenance, single writer, no value echoed)
— it closes the legacy hole where skills free-wrote the budget file. The max budget is a secret
param, masked before any sink, and no read tool ever returns it: only the buyer engine loads it,
inside the store, and its outputs are secret-free (the never-above-max assert is the backstop).
"""

from __future__ import annotations

from sellee.store import StoreError
from sellee.tools.registry import TIER_ATTENDED, ToolContext, ToolError, ToolSpec, register


def _set_budget(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.set_budget(
            params["want_id"],
            params["max_budget"],
            params["source"],
            target_price=params.get("target_price"),
            currency=params.get("currency"),
            force=params.get("force", False),
            opening_ratio=params.get("opening_ratio"),
            auto_counter_step=params.get("auto_counter_step"),
            auto_counter_rounds=params.get("auto_counter_rounds"),
            give_up_polls=params.get("give_up_polls"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _seed(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.buyer_negotiate_seed(
            params["want_id"],
            params["thread_id"],
            params.get("seller", ""),
            listed_price=params.get("listed"),
            our_last=params.get("our_last"),
            seller_ask=params.get("seller_ask"),
            rounds=params.get("rounds"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _open(ctx: ToolContext, params: dict) -> dict:
    if params["listed"] <= 0:
        raise ToolError("listed price must be positive")
    try:
        return ctx.store.buyer_negotiate_open(
            params["want_id"],
            params["thread_id"],
            params.get("seller", ""),
            params["listed"],
            params.get("ask"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _reply(ctx: ToolContext, params: dict) -> dict:
    if params["price"] <= 0:
        raise ToolError("price must be positive")
    try:
        return ctx.store.buyer_negotiate_reply(
            params["want_id"], params["thread_id"], params["price"]
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _accept(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.buyer_negotiate_accept(params["want_id"], params["thread_id"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _walk(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.buyer_negotiate_walk(params["want_id"], params["thread_id"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


register(
    ToolSpec(
        name="set_budget",
        description="Set a want's confidential max budget (and optional target/knobs). The values "
        "are never echoed; the ack reports only provenance and whether an existing budget was "
        "replaced.",
        input_schema={
            "type": "object",
            "properties": {
                "want_id": {"type": "string"},
                "max_budget": {"type": "number"},
                "target_price": {"type": "number"},
                "currency": {"type": "string"},
                "source": {"type": "string", "enum": ["buyer", "default"]},
                "force": {"type": "boolean"},
                "opening_ratio": {"type": "number"},
                "auto_counter_step": {"type": "integer"},
                "auto_counter_rounds": {"type": "integer"},
                "give_up_polls": {"type": "integer"},
            },
            "required": ["want_id", "max_budget", "source"],
            "additionalProperties": False,
        },
        handler=_set_budget,
        tiers=frozenset({TIER_ATTENDED}),
        secret_params=frozenset({"max_budget", "target_price"}),
    )
)
register(
    ToolSpec(
        name="buyer_negotiate_seed",
        description="Seed a buyer ledger entry for a thread the user started by hand, without "
        "emitting an offer or reading the budget.",
        input_schema={
            "type": "object",
            "properties": {
                "want_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "seller": {"type": "string"},
                "listed": {"type": "number"},
                "our_last": {"type": "number"},
                "seller_ask": {"type": "number"},
                "rounds": {"type": "integer"},
            },
            "required": ["want_id", "thread_id"],
            "additionalProperties": False,
        },
        handler=_seed,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="buyer_negotiate_open",
        description="Open a thread with an aggressive offer below list (never above the secret "
        "max budget).",
        input_schema={
            "type": "object",
            "properties": {
                "want_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "seller": {"type": "string"},
                "listed": {"type": "number"},
                "ask": {"type": "number"},
            },
            "required": ["want_id", "thread_id", "listed"],
            "additionalProperties": False,
        },
        handler=_open,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="buyer_negotiate_reply",
        description="Respond to a seller's stated price: climb toward it but strictly under the "
        "max budget, accept when in budget, or walk away.",
        input_schema={
            "type": "object",
            "properties": {
                "want_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "price": {"type": "number"},
            },
            "required": ["want_id", "thread_id", "price"],
            "additionalProperties": False,
        },
        handler=_reply,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="buyer_negotiate_accept",
        description="Commit a struck deal on a thread; returns the deal price and the sibling "
        "threads to close.",
        input_schema={
            "type": "object",
            "properties": {
                "want_id": {"type": "string"},
                "thread_id": {"type": "string"},
            },
            "required": ["want_id", "thread_id"],
            "additionalProperties": False,
        },
        handler=_accept,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="buyer_negotiate_walk",
        description="Walk away from a seller thread (no number revealed).",
        input_schema={
            "type": "object",
            "properties": {
                "want_id": {"type": "string"},
                "thread_id": {"type": "string"},
            },
            "required": ["want_id", "thread_id"],
            "additionalProperties": False,
        },
        handler=_walk,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
