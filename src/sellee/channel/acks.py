"""The two arrivals a typing indicator would lie about.

Everything the seller sends used to earn a receipt here: "Got it: ✅ Accept S$45. I'm working out
what to do — that usually takes a minute or two, sometimes longer. I'll come back to you here."
The same two sentences every time, on every tap and every message. It was written to fix a real
failure — Telegram renders a button press as nothing at all, a seller tapped "Accept S$45", saw a
chat identical to the one before they tapped, and tapped again — but it was never the thing that
fixed it. The keyboard comes off the tapped message in the same tick the tap is read
(`telegram/poller.py` `_spend_the_keyboard`, `discord/gateway.py` `_ack_taps`), and the receipt is
a queued notice that a 2-second lane delivers on a 5-second tick. The strip lands first, every
time. What the receipt added was words, and the words were identical on every arrival.

So the wait is shown rather than narrated: `channel/presence.py` holds the chat's typing indicator
lit for exactly as long as the seller is owed an answer, and nothing is said. What survives here is
the residue — the two arrivals where an indicator would be a lie, and there is no honest way to
show them:

  * **Paused.** `outbound.pulse_typing` returns early while paused, `drain_notices` returns early,
    and `pass_lane` returns before it claims anything. Nothing is running and nothing will run, so
    there is no indicator to light; a seller who hears nothing at all has no way to learn why, and
    no way back. This one carries the Resume door with it.
  * **A tap whose ask cannot be placed** — a token from a withdrawn release, or a notice that
    predates the option that minted it. A pass *is* enqueued, so an indicator would not be false
    about work; it would be false about the outcome, because the row reaches the pass as a bare
    token with nothing to attach it to. Saying so is the only thing that lets the seller recover,
    and the notice is stamped with the pass id so the indicator goes dark as the words arrive —
    otherwise the chat would show "I can't tell what that was answering" under a live "typing…".

Both keep the rules the receipt was written under. One per arrival, not one per row: a pass
coalesces everything pending and answers once, and two sends in the same second is where Telegram's
per-chat rate limit is. Neither says what will be *done* about anything — that belongs to the pass,
which is the only thing that knows.
"""

from __future__ import annotations

import logging

from sellee.channel import fastpaths

log = logging.getLogger(__name__)

PAUSED = "You have me paused, so I won't act on it until you resume."
# A tap whose ask could not be found. The raw token ("a0") must never be echoed as though it were
# words — quoting it would read as the agent having understood something it demonstrably did not.
UNRESOLVED = (
    "I got your tap, but it's from a message I can't place any more, so I can't tell what it was "
    "answering. Tell me in words what you'd like — /catchup shows anything still waiting on you."
)


def ack_arrival(store, rows, *, pass_id: str | None, reply) -> None:
    """Say the one thing this batch is owed, if anything: queue it, or send it direct while paused.

    `rows` are the rows a fast path did not handle — exactly what routes to a channel pass. `reply`
    is the provider's direct send, `reply(text, controls_spec)`, used only on the paused path.
    `pass_id` is the pass this batch is waiting on, used to stamp the notice so the typing indicator
    stops when these words land; None when there is no pass to attribute it to.

    Best-effort by contract: the caller has already committed the rows and advanced its cursor, so
    nothing here may raise past the caller's guard and cost the batch its routing.
    """
    ack = _ack_for(store, rows)
    if ack is None:
        return
    text, controls = ack
    if store.is_paused():
        # The drain lane no-ops while paused, so a queued notice would be held exactly when it is
        # the one thing worth saying. Sent here instead, carrying the way back.
        reply(text, controls)
        return
    store.queue_notice(text, pass_id=pass_id)


def _ack_for(store, rows) -> tuple | None:
    """(text, controls_spec) for the batch, or None when the indicator says it better."""
    if not rows:
        return None
    paused = store.is_paused()
    taps = [row for row in rows if row["kind"] == "action"]
    # The last tap, not every tap: a batch carrying two is a double-tap, and the later one is the
    # live intent. Only its placeability matters — one pass answers the whole batch.
    unplaceable = bool(taps) and not (taps[-1]["payload"] or {}).get("answers_notice_id")
    if not paused:
        return (UNRESOLVED, None) if unplaceable else None
    controls = [(fastpaths.RESUME_LABEL, fastpaths.CB_RESUME)]
    # Both halves, when both apply: the pause is what stops anything happening, and being sent off
    # to answer in words while paused would be being ignored twice.
    return (f"{UNRESOLVED} {PAUSED}" if unplaceable else PAUSED), controls
