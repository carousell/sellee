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
# Telegram's typing indicator lasts ~5s, so a pulse a little under that keeps it alive while a
# channel pass is in flight.
TYPING_PULSE_INTERVAL_SEC = 4.0
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


def pulse_typing(*, store, config, client_factory=None) -> None:
    """Keep the 'typing…' indicator alive while a channel pass is queued or running, so the phone
    feels responsive across the minutes an LLM turn can take. Best-effort: a failed pulse is
    swallowed. No-op while paused, unbound, or with no channel pass in flight."""
    if store.is_paused() or not store.has_active_channel_pass():
        return
    ch = store.get_channel()
    if ch["chat_id"] is None:
        return
    token = secrets.read_telegram_bot_token()
    if not token:
        return
    make = client_factory or _default_client_factory(config)
    try:
        make(token).send_chat_action(ch["chat_id"], "typing")
    except ChannelError as exc:
        log.debug("typing pulse failed (ignored): %s", exc)


def channel_pass_folder(store):
    """A bus subscriber: fold a channel pass's claimed rows when it ends. On ok they are handled;
    on any failure (error/timeout/paused) they are folded failed and one notice is queued — never
    auto-refired (failed rows are terminal; the seller repeating themselves is the recovery)."""

    def _on(event) -> None:
        if event.kind != "pass.end" or event.payload.get("type") != "channel":
            return
        if event.payload.get("class") == "ok":
            store.fold_inbox(event.pass_id, "handled")
            return
        if store.fold_inbox(event.pass_id, "failed"):
            store.queue_notice("I couldn't process your last message — please send it again.")

    return _on


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
