"""Provider-agnostic outbound policy: the notice-drain and typing-pulse *decisions*, the
settled-pass inbox fold, and the escalation-push bus subscriber.

The mechanism — how a message or typing action actually reaches the seller — is the provider's:
`drain_notices` and `pulse_typing` take an injected `deliver(chat_id, text)` / `typing(chat_id)`
callable, so the policy (when bound and not paused, FIFO, bump-and-retry on failure) lives here
once and every provider reuses it.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from selly_agent import settings
from selly_agent.engines import pacing

log = logging.getLogger(__name__)

NOTICE_DRAIN_INTERVAL_SEC = 2.0
INBOX_FOLD_INTERVAL_SEC = 2.0
# Telegram's typing indicator lasts ~5s; a pulse a little under that keeps it alive while a
# channel pass is in flight. Providers without a typing action simply pass a no-op.
TYPING_PULSE_INTERVAL_SEC = 4.0
_NOTICE_DRAIN_BATCH = 10

FAILED_PASS_NOTICE = "I couldn't process your last message — please send it again."


def drain_notices(*, store, bus, deliver, limit: int = _NOTICE_DRAIN_BATCH, now=None) -> None:
    """Deliver queued notices to the bound chat, FIFO, via the provider's `deliver`. No-op while
    paused or unbound (catchup delivers then). During quiet hours only urgent notices (escalation
    pushes) go out — routine ones stay queued and drain at the window's end. A delivery failure
    bumps the notice's attempts (the row stays queued — visible in catchup, never dropped) and
    re-raises so the scheduler backs the lane off."""
    if store.is_paused():
        return
    ch = store.get_channel()
    if ch["chat_id"] is None:
        return
    urgent_only = _in_quiet_hours(store, now)
    for notice in store.claim_queued_notices(limit, urgent_only=urgent_only):
        try:
            deliver(ch["chat_id"], notice["text"], notice["controls"])
        except Exception:
            store.bump_notice_attempts(notice["id"])
            raise
        store.mark_notice_delivered(notice["id"], "channel")
        bus.publish("message.delivered", {"notice_id": notice["id"], "ref": notice["ref"]})


def _in_quiet_hours(store, now) -> bool:
    """Whether the quiet-hours setting's window covers the current daemon-local hour. Evaluated per
    tick against the wall clock (the one-clock rule) — a DST shift moves the window with it, which
    is exactly right for 'don't buzz me at night'."""
    start, end = settings.get(store, "quiet_hours")
    now = time.time() if now is None else now
    return pacing.in_quiet_hours(datetime.fromtimestamp(now).hour, start, end)


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


def fold_settled_passes(*, store) -> None:
    """Fold a channel pass's claimed rows once the pass settles: handled on success; on any
    failure (error/timeout/paused, or a crash the stale sweep failed) folded failed with one
    notice queued — never auto-refired (failed rows are terminal; the seller repeating
    themselves is the recovery).

    A scheduler lane, deliberately not a pass.end subscriber: it derives entirely from durable
    rows (a settled pass that still has claimed rows), so it heals every crash shape where an
    in-process event would have been lost or mis-shaped. The cost is at most one lane interval
    of latency, which nothing downstream notices — delivery itself runs on a lane of the same
    cadence."""
    store.fold_settled_inbox(FAILED_PASS_NOTICE)


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
        # Urgent: an escalation is a decision the seller must make; it bypasses the quiet-hours
        # drain hold (a meetup confirmation shouldn't wait until morning — the seller can mute
        # Telegram themselves if they want silence).
        store.queue_notice(
            f"Needs your call: {esc['open_question']}", ref=esc["thread_id"], urgent=True
        )

    return _on
