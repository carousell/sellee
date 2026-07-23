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

# Unbound, the connect nudge fires once an open escalation has aged past this without a channel to
# push it to. Computed at read time from the escalation's own timestamp — no stored nudge state.
CONNECT_HINT_AGE_SEC = 24 * 3600


def _get_config(ctx: ToolContext, params: dict) -> dict:
    # Every config knob in 03 is non-secret; secrets live in their own files, never in config.
    return dataclasses.asdict(ctx.config)


def _get_status(ctx: ToolContext, params: dict) -> dict:
    hb = heartbeat.read(paths.heartbeat_path())
    now = time.time()
    heartbeat_age = (now - hb["ts"]) if hb and "ts" in hb else None
    uptime = (now - ctx.started_ts) if ctx.started_ts else None
    channel = ctx.store.get_channel()
    return {
        "version": __version__,
        "pid": os.getpid(),
        "uptime_sec": uptime,
        "heartbeat_age_sec": heartbeat_age,
        "carousell_ai_provisioned": secrets.read_carousell_ai_api_key() is not None,
        "queue_depth": ctx.store.count_queued_passes(),
        "open_escalations": ctx.store.count_open_escalations(),
        "channel_bound": channel["chat_id"] is not None,
        "paused": ctx.store.is_paused(),
        "queued_notices": ctx.store.count_queued_notices(),
    }


def _connect_hint(bound: bool, escalations: list, now: float) -> bool:
    if bound:
        return False
    threshold = now - CONNECT_HINT_AGE_SEC
    return any(e["created_ts"] < threshold for e in escalations)


def _get_catchup(ctx: ToolContext, params: dict) -> dict:
    """The needs-me queue: open escalations and queued notices, with counts and channel/pause
    state. Handing the queued notices to this session IS their delivery, so they are stamped
    delivered via catchup; escalations are never stamped by a read (they clear only on resolve)."""
    escalations = ctx.store.list_open_escalations()
    notices = ctx.store.list_queued_notices()
    channel = ctx.store.get_channel()
    bound = channel["chat_id"] is not None
    hint = _connect_hint(bound, escalations, time.time())
    for notice in notices:
        ctx.store.mark_notice_delivered(notice["id"], "catchup")
    return {
        "escalations": escalations,
        "notices": notices,
        "counts": {"escalations": len(escalations), "notices": len(notices)},
        "channel": {"bound": bound, "bot_username": channel["bot_username"]},
        "paused": ctx.store.is_paused(),
        "connect_hint": hint,
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
        name="get_catchup",
        description="The needs-me queue: open escalations awaiting your call and queued updates "
        "(returning the updates delivers them), plus channel/pause state and a connect hint.",
        input_schema=_NO_PARAMS,
        handler=_get_catchup,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
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
