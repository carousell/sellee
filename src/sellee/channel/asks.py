"""Asks with tappable options: what a valid option list is, and what a tap on one *means*.

A decision the seller has to make arrives as a question plus its concrete answers, and those answers
render as buttons on every channel that has them (the provider turns the `(label, token)` spec into
its own widget — see `channel/telegram/commands.render_controls` and
`channel/discord/transport.build_components`). Nothing here is provider-shaped.

Two halves:

  * `validate_options` — the boundary check, shared by the two tools that accept options
    (`escalate`, `send_message`), so an over-long or malformed label fails at the tool call rather
    than at a `sendMessage` the API rejects.
  * `resolve_ask_answers` — the round trip. A tap arrives carrying a token, not words, so this
    swaps in the label the seller actually tapped BEFORE the ingest transaction. Everything
    downstream — the pass prompt, the transcript window, catchup, the `channel.in` event — reads
    `text` off the durable row, so that one rewrite is what makes a tap read as the seller saying
    the thing. Same discipline as the photo download: enrich before ingest, and no later reader
    needs a special path.

A tap is deliberately NOT a fast path. Answering "checkout or handle it myself" means composing a
buyer reply, minting a link, and resolving the escalation — the pass's work, not a deterministic
one. `is_fast_path` already rejects these tokens, so the row stays pending and routes normally.
"""

from __future__ import annotations

import logging

from sellee.channel import controls
from sellee.store import ask_notice_id

log = logging.getLogger(__name__)

# At most four options, matching Telegram's buttons-per-row: these are tapped on a phone, and a
# decision that needs a fifth answer is really a question that hasn't been narrowed yet.
MAX_OPTIONS = 4

# How wide a label may draw. This is a legibility cap, not a send-safety one — the providers accept
# far longer, and that was the problem: a 64-character label passes every API check and renders as
# "Set floor an…" next to "Decline this…". A seller who cannot read a button is guessing, and on a
# price ask a wrong guess declines a live offer or sets a permanent floor.
#
# 24 columns is set by the copy that already exists: every answer set pinned in seller-comms.md
# clears it, the widest being "👍 Not a scam — resume" at 23. Measured in columns rather than
# characters, so an emoji counts for the two it draws — the same measure `channel.controls` packs
# rows by, so a label that passes here is one the renderer can seat.
MAX_OPTION_LABEL_COLS = 24


def validate_options(options) -> list:
    """Check an option list from a tool call and return it as a plain list of labels.

    Raises ValueError with a message the model can act on — the callers turn it into a ToolError.
    Duplicates are rejected because a token resolves by matching its label's position: two identical
    buttons are two doors the seller cannot tell apart, and one of them is unreachable.
    """
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise ValueError("options must be a list of strings")
    labels = [o.strip() for o in options]
    if len(labels) < 2:
        raise ValueError("options needs at least 2 answers — a single button is not a decision")
    if len(labels) > MAX_OPTIONS:
        raise ValueError(
            f"options takes at most {MAX_OPTIONS} answers; narrow the question instead"
        )
    if any(not label for label in labels):
        raise ValueError("every option needs a label")
    if any(controls.display_width(label) > MAX_OPTION_LABEL_COLS for label in labels):
        raise ValueError(
            f"an option label is at most {MAX_OPTION_LABEL_COLS} characters — these are buttons on "
            "a phone, and a longer one renders as an ellipsis the seller has to guess at; put the "
            "detail in the question and keep the button to the answer"
        )
    if any("\n" in label or "\r" in label for label in labels):
        raise ValueError("an option label is a single line")
    if len(set(labels)) != len(labels):
        raise ValueError("two options cannot carry the same label")
    return labels


def resolve_ask_answers(store, events: list) -> list:
    """Return `events` with every ask-answer tap resolved to the words the seller tapped.

    New event dicts — the caller's list is never mutated. An event that is not a tap on an ask, or
    one whose ask cannot be found (an unknown notice, a token from a withdrawn release), is passed
    through untouched: it reaches the pass as the odd message it is, which is recoverable, rather
    than being dropped or guessed at.
    """
    return [_resolved(store, event) for event in events]


def _resolved(store, event: dict) -> dict:
    if event.get("kind") != "action":
        return event
    payload = event.get("payload") or {}
    ref, choice = payload.get("ref"), payload.get("choice")
    notice_id = ask_notice_id(ref)
    if notice_id is None or not choice:
        return event
    answered = store.notice_option(notice_id, f"{ref}:{choice}")
    if answered is None:
        log.info("ask answer %r:%r did not resolve — passing the tap through", ref, choice)
        return event
    return {
        **event,
        # The label becomes the row's text, so the seller's turn reads as what they tapped.
        "text": answered["label"],
        # The ask travels with it: a button lives in the chat forever, so a tap from months-old
        # scrollback has to be able to say which question it was answering.
        "payload": {
            **payload,
            "answers_notice_id": notice_id,
            "answers_text": answered["text"],
        },
    }
