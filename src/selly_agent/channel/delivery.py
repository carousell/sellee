"""Outbound delivery: the notice-drain lane and the escalation-push subscriber.

The drain is the one delivery path when a channel is bound — a scheduler task that claims queued
notices FIFO and sends them, stamping each delivered. Unbound (or paused), it is a no-op and
catchup becomes the delivery path instead, so `send_message`'s behavior never forks on binding
state. A transport failure bumps the notice's attempt count (the row stays queued — visible in
catchup, loud, never silently dropped) and re-raises so the scheduler paces retries with its own
backoff.

The escalation-push subscriber turns every escalation.open into a queued notice, so a bound phone
is buzzed the moment a decision is needed; the crash backstop (a missed notice) is catchup itself.
"""

from __future__ import annotations

import logging

from .. import secrets
from .telegram import ChannelError, TelegramClient

log = logging.getLogger(__name__)

NOTICE_DRAIN_INTERVAL_SEC = 2.0
_NOTICE_DRAIN_BATCH = 10


def _default_client_factory(config):
    def make(token):
        return TelegramClient(token, api_base=config.telegram_api_base)

    return make


def drain_notices(*, store, config, bus, client_factory=None, limit=_NOTICE_DRAIN_BATCH) -> None:
    """Deliver queued notices to the bound chat, FIFO. No-op while paused or unbound (catchup
    delivers then). Raises on a transport failure after bumping the failed notice's attempts, so
    the scheduler backs the lane off."""
    if store.is_paused():
        return
    ch = store.get_channel()
    if ch["chat_id"] is None:
        return
    token = secrets.read_telegram_bot_token()
    if not token:
        return
    make = client_factory or _default_client_factory(config)
    client = make(token)
    for notice in store.claim_queued_notices(limit):
        try:
            client.send_message(ch["chat_id"], notice["text"])
        except ChannelError:
            store.bump_notice_attempts(notice["id"])
            raise
        store.mark_notice_delivered(notice["id"], "channel")
        bus.publish("message.delivered", {"notice_id": notice["id"], "ref": notice["ref"]})


def escalation_notifier(store):
    """A bus subscriber: queue a notice for each new escalation. escalate publishes escalation.open
    exactly once per new escalation (a repeat escalate is idempotent and silent), so this queues
    exactly one notice per escalation. A missed notice still surfaces via catchup."""

    def _on(event) -> None:
        if event.kind != "escalation.open":
            return
        esc = store.get_escalation(event.payload.get("id"))
        if esc is None:  # resolved/pruned between publish and here — catchup covers it
            return
        store.queue_notice(f"Needs your call: {esc['open_question']}", ref=esc["thread_id"])

    return _on
