"""Want tools: create/read, the field-constrained updater, and cancel. The max budget is never
present in any want read — it lives only behind set_budget and the buyer engine.
"""

from __future__ import annotations

from sellee.store import StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)


def _create_want(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.create_want(
            query=params["query"],
            category=params.get("category"),
            condition_pref=params.get("condition_pref"),
            region=params.get("region"),
            currency=params.get("currency"),
            target_price=params.get("target_price"),
            source=params.get("source"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _get_want(ctx: ToolContext, params: dict) -> dict:
    want = ctx.store.get_want(params["want_id"])
    if want is None:
        raise ToolError(f"no want with id {params['want_id']!r}")
    return want


def _list_wants(ctx: ToolContext, params: dict) -> dict:
    return {"wants": ctx.store.list_wants(status=params.get("status"))}


def _update_want(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.update_want(params["want_id"], params["fields"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _cancel_want(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.cancel_want(params["want_id"], params.get("reason"))
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


register(
    ToolSpec(
        name="create_want",
        description="Create a want (a thing the buyer is looking for). The budget is set "
        "separately via set_budget and never lives on the want.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "condition_pref": {"type": "string"},
                "region": {"type": "string"},
                "currency": {"type": "string"},
                "target_price": {"type": "number"},
                "source": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_create_want,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="get_want",
        description="Fetch a want's buyer-safe record (never the max budget).",
        input_schema={
            "type": "object",
            "properties": {"want_id": {"type": "string"}},
            "required": ["want_id"],
            "additionalProperties": False,
        },
        handler=_get_want,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="list_wants",
        description="List want summaries, optionally filtered by status.",
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_list_wants,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="update_want",
        description="Update writable want fields (query, category, condition_pref, region, "
        "currency, target_price, candidates, shortlist). cancelled/bought belong to their flows.",
        input_schema={
            "type": "object",
            "properties": {
                "want_id": {"type": "string"},
                "fields": {"type": "object", "additionalProperties": True},
            },
            "required": ["want_id", "fields"],
            "additionalProperties": False,
        },
        handler=_update_want,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="cancel_want",
        description="Cancel a want and close its open buy threads in one transaction. Refuses a "
        "bought want; never touches the budget.",
        input_schema={
            "type": "object",
            "properties": {
                "want_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["want_id"],
            "additionalProperties": False,
        },
        handler=_cancel_want,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
