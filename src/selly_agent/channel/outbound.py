"""Provider-agnostic outbound policy: the notice-drain and typing-pulse *decisions*, plus the two
pure-store bus subscribers (fold a channel pass's rows, push escalations as notices).

The mechanism — how a message or typing action actually reaches the seller — is the provider's:
`drain_notices` and `pulse_typing` take an injected `deliver(chat_id, text)` / `typing(chat_id)`
callable, so the policy (when bound and not paused, FIFO, bump-and-retry on failure) lives here
once and every provider reuses it.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

NOTICE_DRAIN_INTERVAL_SEC = 2.0
# Telegram's typing indicator lasts ~5s; a pulse a little under that keeps it alive while a
# channel pass is in flight. Providers without a typing action simply pass a no-op.
TYPING_PULSE_INTERVAL_SEC = 4.0
_NOTICE_DRAIN_BATCH = 10


def drain_notices(*, store, bus, deliver, limit: int = _NOTICE_DRAIN_BATCH) -> None:
    """Deliver queued notices to the bound chat, FIFO, via the provider's `deliver`. No-op while
    paused or unbound (catchup delivers then). A delivery failure bumps the notice's attempts (the
    row stays queued — visible in catchup, never dropped) and re-raises so the scheduler backs
    the lane off."""
    if store.is_paused():
        return
    ch = store.get_channel()
    if ch["chat_id"] is None:
        return
    for notice in store.claim_queued_notices(limit):
        try:
            deliver(ch["chat_id"], notice["text"])
        except Exception:
            store.bump_notice_attempts(notice["id"])
            raise
        store.mark_notice_delivered(notice["id"], "channel")
        bus.publish("message.delivered", {"notice_id": notice["id"], "ref": notice["ref"]})


def pulse_typing(*, store, typing) -> None:
    """Keep the seller's chat showing 'typing…' while a channel pass is queued or running, via the
    provider's `typing`. Best-effort: a failed pulse is swallowed. No-op while paused, unbound, or
    with no channel pass in flight."""
    if store.is_paused() or not store.has_active_channel_pass():
        return
    ch = store.get_channel()
    if ch["chat_id"] is None:
        return
    try:
        typing(ch["chat_id"])
    except Exception as exc:
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
