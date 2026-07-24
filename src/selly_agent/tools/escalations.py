"""Escalation tools: open an escalation against a real thread, and resolve it.

escalate flips the thread to escalated and publishes escalation.open; delivery (Telegram push,
attended catchup) is a later workstream's job — here the durable record and the event are what
land. It requires a real, scope-visible thread (no synthetic ids) and is idempotent. resolve
stamps the outcome and publishes escalation.resolved; reactivating the thread stays the caller's
update_thread (escalated->active).
"""

from __future__ import annotations

from selly_agent.store import StoreError
from selly_agent.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)


def _escalate(ctx: ToolContext, params: dict) -> dict:
    try:
        result = ctx.store.escalate(
            params["thread_id"],
            open_question=params["open_question"],
            kind=params.get("kind"),
            context_summary=params.get("context_summary"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    if not result["idempotent"]:
        ctx.bus.publish(
            "escalation.open",
            {"id": result["id"], "thread_id": params["thread_id"], "kind": params.get("kind")},
            pass_id=ctx.session.pass_id,
        )
    return result


def _resolve_escalation(ctx: ToolContext, params: dict) -> dict:
    try:
        result = ctx.store.resolve_escalation(params["escalation_id"], params["resolution"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    ctx.bus.publish(
        "escalation.resolved",
        {"id": params["escalation_id"]},
        pass_id=ctx.session.pass_id,
    )
    return result


register(
    ToolSpec(
        name="escalate",
        description="Open an escalation against a real thread (records the open question, flips "
        "the thread to escalated). Idempotent: a thread with an open escalation returns it.",
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "open_question": {"type": "string"},
                "kind": {"type": "string"},
                "context_summary": {"type": "string"},
            },
            "required": ["thread_id", "open_question"],
            "additionalProperties": False,
        },
        handler=_escalate,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="resolve_escalation",
        description="Mark an escalation resolved with an outcome note. Reactivating the thread is "
        "a separate update_thread (escalated->active).",
        input_schema={
            "type": "object",
            "properties": {
                "escalation_id": {"type": "string"},
                "resolution": {"type": "string"},
            },
            "required": ["escalation_id", "resolution"],
            "additionalProperties": False,
        },
        handler=_resolve_escalation,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
