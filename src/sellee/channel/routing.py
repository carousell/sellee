"""Provider-agnostic ingest fan-out: publish the observability event for an inbound row, route
pending rows to a coalesced channel pass, and settle a freshly-ingested batch.

A provider's receive loop calls `settle_batch` after it has ingested a batch into the durable inbox
and answered whatever a fast path could. Everything the tail does — routing, the seller's receipt,
the typing pulse — is the same decision on every provider, and the ordering between those steps
carries reasons that would be restated or quietly lost in a second copy. So it lives here once, and
each loop passes in its own `reply` / `typing` closures: the same injection shape `outbound`
already uses for the delivery lanes.
"""

from __future__ import annotations

import logging

from sellee.channel import acks, outbound

log = logging.getLogger(__name__)

_CHANNEL_IN_PREVIEW_CAP = 200


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


def settle_batch(store, bus, routed, *, reply, typing) -> None:
    """Finish one ingested batch: route it, receipt it, then show the agent working.

    `routed` is the rows a fast path did not handle — exactly what a channel pass will sweep. The
    order is not arrangeable:

      * **Route first.** The ingest transaction has already committed the rows *and* advanced the
        provider's cursor, so the batch can never be redelivered. A receipt that hangs or throws
        before the routing call would leave rows waiting on the lane's next tick at best.
      * **Receipt before typing.** Both Telegram and Discord clear the typing indicator the instant
        the bot sends a message, so pulsing first spends the pulse on nothing.

    `pass_was_active` is read before routing because after it the answer is always yes — what the
    receipt needs to know is whether the seller had already been told the agent was working.
    """
    if not routed:
        return
    pass_was_active = store.has_active_channel_pass()
    route_channel_pass(store, bus)
    try:
        acks.ack_arrival(store, routed, pass_was_active=pass_was_active, reply=reply)
    except Exception:
        # Blanket, not ChannelError: the transports do not wrap `json.loads`, so a captive portal
        # answering 200 with HTML raises straight past a transport-shaped guard. A receipt is
        # cosmetic and this runs on the receive thread — it must never cost the loop its tick.
        log.exception("arrival receipt failed (the batch is routed either way)")
    outbound.pulse_typing(store=store, typing=typing)
