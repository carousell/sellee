"""send_reply and record_manual_reply — the send bracket, composed atomically in code.

The legacy five-CLI bracket (pacing reserve → journal intent → browser type+send → mark-sent →
commit, restated across five prompt files) ceases to exist as instructions. send_reply runs it as
one tool call: validate the thread, reserve pacing + write a durable intent (one transaction),
send through the sink outside any transaction, then fold the outbound row + advance the cursor +
mark the intent committed (a second transaction). A wait/quiet verdict records nothing; a killed
send leaves the intent for the sweep to fold as unconfirmed, never a re-send. 04 ships no live sink,
so a real market returns a structured no_send_path.
"""

from __future__ import annotations

import time

from ..engines import pacing as pacing_engine
from ..store import StoreError
from .registry import (
    TIER_ATTENDED,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_KINDS = ("reply", "holding", "followup", "nudge")

# Terminal / owned statuses a reply must never re-engage. agreed is included on the sell side —
# post-agreement coordination belongs to the close / sale-watch flows, not a reply.
_SELL_REFUSED = frozenset(
    {"lost", "handover", "closed", "escalated", "held", "agreed", "seller_handling"}
)
_BUY_REFUSED = frozenset({"closed", "escalated", "held"})


def _refused(side: str, status: str, kind: str) -> bool:
    if side == "sell":
        return status in _SELL_REFUSED
    if status in _BUY_REFUSED:
        return True
    # a buy thread stays conversational at `agreed` for replies, but not for follow-up nudges
    return kind == "followup" and status == "agreed"


def _send_reply(ctx: ToolContext, params: dict) -> dict:
    kind = params.get("kind", "reply")
    thread = ctx.store.get_thread(params["thread_id"])
    if thread is None:
        raise ToolError(f"no thread with id {params['thread_id']!r}")
    if _refused(thread["side"], thread["status"], kind):
        raise ToolError(
            f"thread is {thread['status']!r} — not eligible for a {kind} "
            "(terminal/held/escalated threads are never re-engaged)"
        )

    # 04 ships no live sink: refuse before any reserve or intent, so nothing is recorded.
    if ctx.reply_sink is None:
        return {
            "status": "no_send_path",
            "thread_id": params["thread_id"],
            "market": thread["market"],
        }

    cfg = pacing_engine.resolve(ctx.config)
    interactive = ctx.session.tier == TIER_ATTENDED
    try:
        reserved = ctx.store.reserve_reply(
            thread_id=params["thread_id"],
            kind=kind,
            text=params["text"],
            in_msg_id=params.get("in_msg_id"),
            cfg=cfg,
            interactive=interactive,
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    if reserved["verdict"] != "go":
        # a blocked verdict created no intent, no transcript row, no pacing row
        return {"status": reserved["verdict"], "delay_sec": reserved["delay_sec"]}

    # the anti-automation jitter is slept here, after the reserve transaction — never under the lock
    if reserved["delay_sec"] > 0:
        time.sleep(reserved["delay_sec"])

    intent_id = reserved["intent_id"]
    try:
        ctx.reply_sink.send(thread, params["text"], kind)
    except Exception:
        # the intent stays pending; the sweep folds it as unconfirmed + escalates — never re-sent
        return {"status": "send_failed", "intent_id": intent_id}

    commit = ctx.store.commit_reply(
        intent_id=intent_id,
        thread_id=params["thread_id"],
        in_msg_id=params.get("in_msg_id"),
        text=params["text"],
        kind=kind,
    )
    return {"status": "sent", "intent_id": intent_id, "msg_id": commit["msg_id"]}


def _record_manual_reply(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.record_manual_reply(
            params["thread_id"], params["text"], handle=params.get("handle")
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


register(
    ToolSpec(
        name="send_reply",
        description="Send a reply on a marketplace thread: pacing + durable intent + send + cursor "
        "advance, composed atomically. Blocked verdicts record nothing; kinds: "
        "reply|holding|followup|nudge.",
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": list(_KINDS)},
                "in_msg_id": {"type": "string"},
            },
            "required": ["thread_id", "text"],
            "additionalProperties": False,
        },
        handler=_send_reply,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="record_manual_reply",
        description="Journal a reply the seller sent themselves in the marketplace app (deduped by "
        "text; no cursor advance) so follow-ups stop treating the buyer as unanswered.",
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "text": {"type": "string"},
                "handle": {"type": "string"},
            },
            "required": ["thread_id", "text"],
            "additionalProperties": False,
        },
        handler=_record_manual_reply,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
