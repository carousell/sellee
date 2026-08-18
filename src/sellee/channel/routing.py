"""Provider-agnostic ingest fan-out: publish the observability event for an inbound row, and route
pending free-text rows to a coalesced channel pass. A provider's receive loop calls these after it
has ingested a batch into the durable inbox — they touch only the store and the event bus.
"""

from __future__ import annotations

_CHANNEL_IN_PREVIEW_CAP = 200


def publish_channel_in(bus, row) -> None:
    preview = (row["text"] or "")[:_CHANNEL_IN_PREVIEW_CAP]
    bus.publish("channel.in", {"kind": row["kind"], "preview": preview, "src_ts": row["src_ts"]})


def route_channel_pass(store, bus) -> None:
    """Coalescing route: the store enqueues a channel pass only when pending rows exist and none is
    already queued/running, so one pass sweeps everything pending and later arrivals wait."""
    pass_id = store.enqueue_channel_pass()
    if pass_id is not None:
        bus.publish("pass.queued", {"type": "channel"}, pass_id=pass_id)
