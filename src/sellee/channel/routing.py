"""Provider-agnostic ingest fan-out: publish the observability event for an inbound row, route
pending rows to a coalesced channel pass, and settle a freshly-ingested batch.

A provider's receive loop calls `settle_batch` after it has ingested a batch into the durable inbox
and answered whatever a fast path could. Everything the tail does — routing, the one thing an
arrival is sometimes owed in words, the typing pulse — is the same decision on every provider, and
the ordering between those steps carries reasons that would be restated or quietly lost in a second
copy. So it lives here once, and each loop passes in its own `reply` / `typing` closures: the same
injection shape `outbound` already uses for the delivery lanes.
"""

from __future__ import annotations

import logging

from sellee.channel import acks, outbound

log = logging.getLogger(__name__)

_CHANNEL_IN_PREVIEW_CAP = 200

# What the seller sees on their own message the moment it lands: eyes, meaning "seen, reading it".
#
# It replaces the sentence that used to be sent back ("Got it: …") and is better than it at the one
# thing that sentence did well. It attaches to the *specific* message, so a burst of three shows
# which ones landed, where a chat-level indicator cannot. It survives in the scrollback, where the
# indicator is gone in seconds. And it adds no bubble, no notification and nothing to read.
#
# Eyes and not a thumb: at this point the message has been read and nothing else. A thumbs-up reads
# as agreement to a question no tool has looked at yet, which is the same over-claim the old receipt
# was carefully written to avoid.
#
# Must be a reaction the platform allows — Telegram rejects an emoji outside its own set at send.
SEEN_REACTION = "👀"


def publish_channel_in(bus, row) -> None:
    preview = (row["text"] or "")[:_CHANNEL_IN_PREVIEW_CAP]
    bus.publish("channel.in", {"kind": row["kind"], "preview": preview, "src_ts": row["src_ts"]})


def route_channel_pass(store, bus) -> str | None:
    """Coalescing route: the store enqueues a channel pass only when pending rows exist and none is
    already queued/running, so one pass sweeps everything pending and later arrivals wait for the
    next. Returns the pass_id it enqueued, or None.

    Called both from a provider's ingest tail (so the common case is same-tick) and from the
    `channel_route` scheduler lane (so a row that waited out a pass is never left for the seller to
    dislodge by speaking again)."""
    pass_id = store.enqueue_channel_pass()
    if pass_id is not None:
        bus.publish("pass.queued", {"type": "channel"}, pass_id=pass_id)
    return pass_id


def settle_batch(store, bus, inserted, handled, *, reply, typing, react) -> None:
    """Finish one ingested batch: route it, mark it seen, say anything it is owed in words, then
    show the wait.

    `inserted` is everything the ingest transaction committed; `handled` is the ids a fast path
    already answered. The split is computed here rather than by each caller — it is the same
    comprehension on both providers, and the two halves are not used for the same thing:

      * **Route, react and speak for the remainder only.** A fast path has already replied, and its
        reply *is* the acknowledgement — a reaction on a message that was answered in the same tick
        marks nothing, and a second word after "Paused" would contradict it.
      * **Pulse for the whole batch.** A fast path answering `/status` mid-pass sends a message,
        and both platforms clear the typing indicator the instant the bot sends anything — so the
        pulse is owed even on a batch this tail routes nothing from, or the indicator stays dark
        until the keeper's next refresh while a pass is still working.

    The rest of the order is not arrangeable:

      * **Route first.** The ingest transaction has already committed the rows *and* advanced the
        provider's cursor, so the batch can never be redelivered. A send that hangs or throws
        before the routing call would leave rows waiting on the lane's next tick at best.
      * **Speak before pulsing**, in the two cases anything is said at all. Paused, there is no
        pulse to spend — `pulse_typing` returns early. Unplaceable, the words and the indicator
        contradict each other, so the words go first and the notice carries the pass id that stops
        the indicator re-arming behind them.

    The reaction sits anywhere in that order, which is the nice property of it: unlike a message, it
    does not clear the typing indicator on either platform, so it cannot spend a pulse. It goes
    here, right after routing, because it is the fastest acknowledgement available and the seller
    should have it before anything slower is attempted.
    """
    if not inserted:
        return
    routed = [row for row in inserted if row["id"] not in handled]
    if routed:
        pass_id = route_channel_pass(store, bus)
        if pass_id is None:
            # A pass was already in flight, so these rows stayed pending and will be swept by the
            # next one. What an ack needs is the pass the seller is actually waiting on now.
            active = store.active_channel_pass()
            pass_id = active["pass_id"] if active else None
        mark_seen(routed, react=react)
        try:
            acks.ack_arrival(store, routed, pass_id=pass_id, reply=reply)
        except Exception:
            # Blanket, not ChannelError: the transports do not wrap `json.loads`, so a captive
            # portal answering 200 with HTML raises straight past a transport-shaped guard. This
            # runs on the receive thread — it must never cost the loop its tick.
            log.exception("arrival ack failed (the batch is routed either way)")
    outbound.pulse_typing(store=store, typing=typing)


def mark_seen(rows, *, react) -> None:
    """React to each of the seller's own messages in the batch, via the provider's `react`.

    Every row, not one per batch: the reaction's whole advantage over the sentence it replaces is
    that it is per-message, so three messages sent in a burst each get their own mark and the seller
    can tell that none of them was missed.

    Never a tap. A tap is a callback rather than a message, so there is nothing of the seller's to
    mark — and the `message_id` its payload carries is the *agent's* own message, the one the button
    sits on, kept there so the keyboard can be taken off it. Reacting to that would be the agent
    marking its own message as seen. A tap has its own instant feedback anyway, in that keyboard
    coming off.

    Best-effort per row, and the log stays at debug: this runs on a receive thread where the batch
    is already committed and routed, and on Telegram a reaction is also the one call here that can
    fail for an ordinary reason rather than a broken one — the Bot API refuses to react to a message
    old enough to have aged out, which a catchup-driven burst can reach.
    """
    for row in rows:
        if row["kind"] == "action":
            continue
        message_id = (row["payload"] or {}).get("message_id")
        if message_id is None:
            continue
        try:
            react(message_id)
        except Exception as exc:
            log.debug("marking a message seen failed (ignored): %s", exc)
