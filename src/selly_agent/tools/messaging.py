"""send_message — queue-and-catchup, never pretend-push.

The handler inserts a durable notice row and publishes message.out; delivery is a separate concern
(the notice-drain lane sends it to the bound channel, or catchup hands it over when unbound). The
tool's behavior never forks on binding state — a send always succeeds and always queues, so an
unbound channel simply accumulates notices that catchup surfaces later.
"""

from __future__ import annotations

from .registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    ToolContext,
    ToolSpec,
    register,
)


def _send_message(ctx: ToolContext, params: dict) -> dict:
    notice_id = ctx.store.queue_notice(
        params["text"], ref=params.get("ref"), pass_id=ctx.session.pass_id
    )
    payload = {"text": params["text"], "notice_id": notice_id}
    if "ref" in params:
        payload["ref"] = params["ref"]
    ctx.bus.publish("message.out", payload, pass_id=ctx.session.pass_id)
    return {"queued": True, "notice_id": notice_id}


register(
    ToolSpec(
        name="send_message",
        description="Queue a message to the seller's channel. It is delivered when a channel is "
        "bound, or surfaced at catchup when not — a send never fails for lack of a channel.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "ref": {"type": "string"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=_send_message,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_PUBLISH, TIER_PASS_CHANNEL}),
    )
)
