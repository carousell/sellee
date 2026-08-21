"""send_reply and record_manual_reply — the send bracket, composed atomically in code.

send_reply is the whole bracket in one tool call: validate the thread, acquire the sink (which
starts Chrome if a closed window is all that is missing), reserve pacing + write a durable intent
(one transaction), send through the sink outside any transaction, then fold the outbound row +
advance the cursor + mark the intent committed (a second transaction). A wait/quiet verdict records
nothing; a killed send leaves the intent for the sweep to fold as unconfirmed, never a re-send.
With no way to send, it returns a structured no_send_path with nothing recorded.
"""

from __future__ import annotations

import time

from sellee import settings
from sellee.browser.client import BrowserError
from sellee.engines import pacing as pacing_engine
from sellee.store import StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_KINDS = ("reply", "holding", "followup", "nudge")

# Terminal / owned statuses a reply must never re-engage.
_SELL_REFUSED = frozenset(
    {"lost", "handover", "closed", "escalated", "held", "agreed", "seller_handling"}
)
_BUY_REFUSED = frozenset({"closed", "escalated", "held"})


def _refused(side: str, status: str, kind: str) -> bool:
    if side == "sell":
        # `agreed` opens for a plain reply and nothing else. The price is settled, and what the
        # buyer is still owed is the checkout link — so relaying it is a reply, while a nudge or a
        # fresh negotiation on a done deal is not. The buy side has always worked this way.
        if status == "agreed":
            return kind != "reply"
        return status in _SELL_REFUSED
    if status in _BUY_REFUSED:
        return True
    # a buy thread stays conversational at `agreed` for replies, but not for follow-up nudges
    return kind == "followup" and status == "agreed"


def _send_reply(ctx: ToolContext, params: dict) -> dict:
    # A paused agent acts on nothing — even an attended session's send respects the pause. Refuse
    # before any thread load or reserve so nothing is recorded.
    if ctx.store.is_paused():
        return {"status": "paused", "thread_id": params["thread_id"]}
    kind = params.get("kind", "reply")
    thread = ctx.store.get_thread(params["thread_id"])
    if thread is None:
        raise ToolError(f"no thread with id {params['thread_id']!r}")
    if _refused(thread["side"], thread["status"], kind):
        raise ToolError(
            f"thread is {thread['status']!r} — not eligible for a {kind} "
            "(terminal/held/escalated threads are never re-engaged)"
        )

    # Acquire the send path before any reserve or intent, so "no browser" is refused with nothing
    # recorded: no pacing slot spent, and no pending intent for the sweep to escalate as a send
    # nobody can verify — this send provably never happened.
    try:
        sink = ctx.reply_sink() if ctx.reply_sink is not None else None
    except BrowserError as exc:
        return {
            "status": "no_send_path",
            "thread_id": params["thread_id"],
            "market": thread["market"],
            "detail": str(exc),
        }
    if sink is None:
        return {
            "status": "no_send_path",
            "thread_id": params["thread_id"],
            "market": thread["market"],
        }

    cfg = pacing_engine.resolve(ctx.config, settings.quiet_window_minutes(ctx.store))
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
        sink.send(thread, params["text"], kind, intent_id)
    except Exception:
        # The sink's own message never reaches here by design (it emits its own events). Whether
        # the message may be retried is the intent's durable status, and the return says which
        # case this is: still `pending` means nothing was delivered and a retry is safe;
        # `sent_unverified` means the page took it, and the sweep asks a human rather than
        # anyone sending it again.
        if ctx.store.intent_status(intent_id) == "sent_unverified":
            return {"status": "send_unverified", "intent_id": intent_id}
        return {"status": "send_failed", "intent_id": intent_id}

    commit = ctx.store.commit_reply(
        intent_id=intent_id,
        thread_id=params["thread_id"],
        in_msg_id=params.get("in_msg_id"),
        text=params["text"],
        kind=kind,
        pass_id=ctx.session.pass_id,
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
        "reply|holding|followup|nudge. Statuses: send_failed = nothing was delivered, retrying is "
        "safe; send_unverified = the page took it but it could not be confirmed — never resend, an "
        "escalation follows; unverified_open = an earlier send on this thread is still "
        "unconfirmed, wait for its escalation to be resolved; scam_flagged = this thread carries a "
        "scam signal and is the seller's to answer, never yours.",
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
        # The channel pass sends too: when the seller answers a question or picks how to close, the
        # answer has to reach the buyer, and it is the flow holding the seller's words.
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_REPLY, TIER_PASS_CHANNEL}),
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
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
