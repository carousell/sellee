"""send_message — queue-and-catchup, never pretend-push.

The handler inserts a durable notice row and publishes message.out; delivery is a separate concern
(the notice-drain lane sends it to the bound channel, or catchup hands it over when unbound). The
tool's behavior never forks on binding state — a send always succeeds and always queues, so an
unbound channel simply accumulates notices that catchup surfaces later.
"""

from __future__ import annotations

from sellee.channel import asks
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)


def _send_message(ctx: ToolContext, params: dict) -> dict:
    options = params.get("options")
    if options is not None:
        try:
            options = asks.validate_options(options)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
    notice_id = ctx.store.queue_notice(
        params["text"], ref=params.get("ref"), pass_id=ctx.session.pass_id, options=options
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
        "bound, or surfaced at catchup when not — a send never fails for lack of a channel. When "
        "the message asks the seller to choose, pass `options` so they can tap instead of type.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "ref": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For a message that asks the seller to choose: the 2-4 "
                    "concrete answers, each short enough for a button on a phone. Omit for an "
                    "ordinary message — buttons on something that isn't a question read as one.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=_send_message,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_PUBLISH, TIER_PASS_CHANNEL}),
    )
)
