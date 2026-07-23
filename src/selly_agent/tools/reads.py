"""Read tools: config, daemon status, and the buyer-safe item views. None return a floor."""

from __future__ import annotations

import dataclasses
import os
import time

from .. import __version__, heartbeat, paths, secrets
from .registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_NO_PARAMS = {"type": "object", "properties": {}, "additionalProperties": False}


def _get_config(ctx: ToolContext, params: dict) -> dict:
    # Every config knob in 03 is non-secret; secrets live in their own files, never in config.
    return dataclasses.asdict(ctx.config)


def _get_status(ctx: ToolContext, params: dict) -> dict:
    hb = heartbeat.read(paths.heartbeat_path())
    now = time.time()
    heartbeat_age = (now - hb["ts"]) if hb and "ts" in hb else None
    uptime = (now - ctx.started_ts) if ctx.started_ts else None
    return {
        "version": __version__,
        "pid": os.getpid(),
        "uptime_sec": uptime,
        "heartbeat_age_sec": heartbeat_age,
        "carousell_ai_provisioned": secrets.read_carousell_ai_api_key() is not None,
        "queue_depth": ctx.store.count_queued_passes(),
        "open_escalations": ctx.store.count_open_escalations(),
    }


def _get_item(ctx: ToolContext, params: dict) -> dict:
    item = ctx.store.get_item(params["item_id"])
    if item is None:
        raise ToolError(f"no item with id {params['item_id']!r}")
    return item


def _list_items(ctx: ToolContext, params: dict) -> dict:
    return {"items": ctx.store.list_items(status=params.get("status"))}


register(
    ToolSpec(
        name="get_config",
        description="Return the non-secret daemon configuration knobs.",
        input_schema=_NO_PARAMS,
        handler=_get_config,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="get_status",
        description="Report daemon health: version, pid, uptime, heartbeat freshness, "
        "whether the carousell.ai rail is provisioned, and the pass queue depth.",
        input_schema=_NO_PARAMS,
        handler=_get_status,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="get_item",
        description="Fetch one item's buyer-safe record (never its floor).",
        input_schema={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
        handler=_get_item,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_PUBLISH, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="list_items",
        description="List item summaries, optionally filtered by status.",
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_list_items,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
