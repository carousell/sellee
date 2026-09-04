"""What the seller hears the moment something they sent lands — before the pass that answers it.

A channel pass takes a while. Measured over 27 of them: 31.6s at the fastest, ~80s median, ~155s at
p75, with a tail past five minutes. `pass_deadline_sec` is a kill ceiling, not an expectation.

For a typed message that silence is merely long. For a *tap* it was total: Telegram does not render
a button press as anything, and `answerCallbackQuery` only stops a spinner nobody sees. A seller
tapped "Accept S$45", saw the chat sit exactly as it had before, and reasonably concluded the tap
had not registered — then tapped again. Two rows, one second apart, and the second was stranded.

So this is the receipt. It is deliberately thin: it names what arrived and how long the wait is, and
it never says what will be *done* about it. That belongs to the pass, which is the only thing that
knows — a pre-written "Accepted, I've told them yes" would be a claim made before any tool ran, and
the reply lane already has a post-mortem about reporting sends that never happened.

Two rules shape the rest:

  * **One receipt per arrival, not per row.** A pass coalesces everything pending and answers once,
    so N receipts would promise N answers. It also keeps a double-tap from putting two sends into
    the same second, which is where Telegram's per-chat rate limit is.
  * **It is queued, not sent.** `recent_transcript` reads the notices table with no status filter,
    so a queued receipt is in the pass's own window immediately — the pass sees it acknowledged and
    does not repeat it, with nothing added to the prompt to ask it not to. Queuing also buys retry,
    catchup, and the send moving off the receive thread. The one exception is a paused agent, whose
    drain lane deliberately does not run; there the receipt goes direct, exactly as the other
    seller-initiated fast paths already reply while paused.
"""

from __future__ import annotations

import logging

from sellee import prompt_data
from sellee.channel import fastpaths

log = logging.getLogger(__name__)

# Composable halves, so four states need four strings rather than four templates.
#
# The wait clause is the load-bearing half and it is measured, not guessed: "a minute or two" is
# true at the median (~80s) and p75 (~155s), and "sometimes longer" is what makes the tail honest
# instead of a broken promise. Never a seconds claim — the fastest pass observed was 31.6s — and
# never the 900s deadline, which is a timeout rather than a duration. The house rule against
# implying speed ("one sec", "shortly", "right away") is in voice-and-style.md; this is the same
# rule with a number behind it.
HEARD = "Got it: {label}."
WORKING = (
    "I'm working out what to do — that usually takes a minute or two, sometimes longer. I'll come "
    "back to you here."
)
PAUSED = "You have me paused, so I won't act on it until you resume."
# A tap whose ask could not be found: a token from a withdrawn release, or a notice that predates
# the option that minted it. The raw token ("a0") must never be echoed as though it were words.
UNRESOLVED = (
    "I got your tap, but it's from a message I can't place any more, so I can't tell what it was "
    "answering. Tell me in words what you'd like — /catchup shows anything still waiting on you."
)


def ack_arrival(store, rows, *, pass_was_active: bool, reply) -> None:
    """Receipt one batch of routed rows: queue it, or send it directly when paused.

    `rows` are the rows a fast path did not handle — exactly what routes to a channel pass. `reply`
    is the provider's direct send, `reply(text, controls_spec)`, used only on the paused path.

    Best-effort by contract: the caller has already committed the rows and advanced its cursor, so
    nothing here may raise past the caller's guard and cost the batch its routing.
    """
    ack = _ack_for(store, rows, pass_was_active=pass_was_active)
    if ack is None:
        return
    text, controls = ack
    if store.is_paused():
        # The drain lane no-ops while paused, so a queued receipt would be held exactly when it is
        # the one thing worth saying. Sent here instead, carrying the way back.
        reply(text, controls)
        return
    store.queue_notice(text)


def _ack_for(store, rows, *, pass_was_active: bool) -> tuple | None:
    """(text, controls_spec) for the batch, or None when no receipt is owed."""
    if not rows:
        return None
    paused = store.is_paused()
    taps = [row for row in rows if row["kind"] == "action"]
    if not taps and not paused and pass_was_active:
        # A typed message during a pass that is already running: they have been told once, and this
        # arrival will be swept by the same conversation. Saying it again is noise, not reassurance.
        return None
    controls = [(fastpaths.RESUME_LABEL, fastpaths.CB_RESUME)] if paused else None
    tail = PAUSED if paused else WORKING
    if not taps:
        return tail, controls
    # The last tap, not every tap: a batch carrying two is the double-tap this receipt exists to
    # prevent, and both carry the same label. Where they differ, the later one is the live intent.
    last = taps[-1]
    if not (last["payload"] or {}).get("answers_notice_id"):
        return f"{UNRESOLVED} {tail}" if paused else UNRESOLVED, controls
    # Newline-free by validation; one_line is belt-and-braces against a label that predates it,
    # since a break here would stage a second message nobody wrote.
    return f"{HEARD.format(label=prompt_data.one_line(last['text']))} {tail}", controls
