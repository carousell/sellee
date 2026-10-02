"""Write tools: create/update items and set an item's floor.

update_item's field constraint and set_floor's discipline both live in the store (the single
writer); these handlers translate the store's typed failures into caller-facing tool errors.
set_floor's floor parameter is marked secret, so the dispatch path masks it before any sink.
"""

from __future__ import annotations

from sellee.store import StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)


def _create_item(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.create_item(
            title=params["title"],
            list_price=params["list_price"],
            # Left out, it is the currency the seller sells in: "dollars" says no more than that.
            currency=params.get("currency") or ctx.store.seller_currency(),
            description=params.get("description", ""),
            condition=params.get("condition"),
            photos=params.get("photos"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _update_item(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.update_item(params["item_id"], params["fields"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _set_floor(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.set_floor(
            params["item_id"],
            params["floor"],
            params["source"],
            force=params.get("force", False),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


register(
    ToolSpec(
        name="create_item",
        description="Create a draft item; the server assigns its id. Photos are paths already "
        "inside the media store — from a channel photo message, or from import_photos.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "list_price": {"type": "number"},
                "currency": {
                    "type": "string",
                    "description": "ISO 4217 code. Leave it out unless the seller named a "
                    "currency; it then defaults to the one the seller sells in.",
                },
                "description": {"type": "string"},
                "condition": {"type": "string"},
                "photos": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "list_price"],
            "additionalProperties": False,
        },
        handler=_create_item,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="update_item",
        description="Update a draft item's fields (title, description, condition, list_price, "
        "currency, photos, and draft<->ready status). Photos are paths already inside the media "
        "store. Changes the record only, never a listing: on an item that is already listed it "
        "refuses anything a buyer sees — use update_live_listing for that. Listing URLs and sale "
        "states are not writable here.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "fields": {"type": "object", "additionalProperties": True},
            },
            "required": ["item_id", "fields"],
            "additionalProperties": False,
        },
        handler=_update_item,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="set_floor",
        description="Set an item's confidential floor. The value is never echoed; the ack "
        "reports only provenance and whether an existing floor was replaced.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "floor": {"type": "number"},
                "source": {"type": "string", "enum": ["seller", "default"]},
                "force": {"type": "boolean"},
            },
            "required": ["item_id", "floor", "source"],
            "additionalProperties": False,
        },
        handler=_set_floor,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
        secret_params=frozenset({"floor"}),
    )
)
