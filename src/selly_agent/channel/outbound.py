"""Provider-agnostic outbound policy: the notice-drain and typing-pulse *decisions*, the
settled-pass inbox fold, the escalation-push bus subscriber, and the daemon-authored onboarding
messages (the bind welcome and the first-listing nudge).

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
from selly_agent.channel import fastpaths
from selly_agent.engines import pacing

log = logging.getLogger(__name__)

NOTICE_DRAIN_INTERVAL_SEC = 2.0
INBOX_FOLD_INTERVAL_SEC = 2.0
# Telegram's typing indicator lasts ~5s; a pulse a little under that keeps it alive while a
# channel pass is in flight. Providers without a typing action simply pass a no-op.
TYPING_PULSE_INTERVAL_SEC = 4.0
_NOTICE_DRAIN_BATCH = 10

FAILED_PASS_NOTICE = "I couldn't process your last message — please send it again."

# The bind greeting, and — when nothing is listed yet — the activation moment: point the seller
# at their first listing. The CTA never fires for a seller with real items (a rebind after real
# usage must not say "your first"), and its timing promise is honest: a listing takes minutes.
WELCOME_TEXT = (
    "You're connected. I list what you're selling, talk to buyers, and bring you the decisions "
    "that need your call. Send /selly any time to see your settings and what's waiting."
)
FIRST_LISTING_CTA_TEXT = (
    "Ready when you are: send a photo of something you want to sell. I'll look up what similar "
    "ones sold for and suggest a price — nothing goes live until you've OK'd it. The whole thing "
    "takes a few minutes."
)
CTA_SKIP_LABEL = "Skip for now"

FIRST_LISTING_NUDGE_TEXT = (
    "Whenever you're ready: send a photo of something you want to sell. I'll research the price "
    "and check it with you before anything goes live. I won't ask again."
)
FIRST_LISTING_NUDGE_REF = "first-listing-nudge"
FIRST_LISTING_NUDGE_AGE_SEC = 24 * 3600
FIRST_LISTING_NUDGE_INTERVAL_SEC = 3600.0


def drain_notices(*, store, bus, deliver, limit: int = _NOTICE_DRAIN_BATCH, now=None) -> None:
    """Deliver queued notices to the bound chat, FIFO, via the provider's `deliver`. No-op while
    paused or unbound (catchup delivers then). During quiet hours only *proactive* notices are held
    — seller-facing ones (channel-pass replies, settings approvals, escalation pushes) deliver at
    any hour, so seller-initiated chat is never gated. A delivery failure bumps the notice's
    attempts (the row stays queued — visible in catchup, never dropped) and re-raises so the
    scheduler backs the lane off."""
    if store.is_paused():
        return
    ch = store.get_channel()
    if ch["chat_id"] is None:
        return
    in_quiet = _in_quiet_hours(store, now)
    for notice in store.claim_queued_notices(limit, in_quiet=in_quiet):
        try:
            deliver(ch["chat_id"], notice["text"], notice["controls"])
        except Exception:
            store.bump_notice_attempts(notice["id"])
            raise
        store.mark_notice_delivered(notice["id"], "channel")
        bus.publish("message.delivered", {"notice_id": notice["id"], "ref": notice["ref"]})


def _in_quiet_hours(store, now) -> bool:
    """Whether the quiet-hours setting's window (minutes since midnight) covers the current
    daemon-local time. Evaluated per tick against the wall clock (the one-clock rule) — a DST shift
    moves the window with it, which is exactly right for 'don't buzz me at night'."""
    start_min, end_min = settings.quiet_window_minutes(store)
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(now)
    return pacing.in_quiet_window(dt.hour * 60 + dt.minute, start_min, end_min)


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
        # An escalation is a decision the seller must make; it is not holdable, so it delivers at
        # any hour (a meetup confirmation shouldn't wait until morning — the seller can mute
        # Telegram themselves if they want silence). holdable defaults to False, so this is just a
        # plain queue_notice — spelled out here because the non-hold is a deliberate policy.
        store.queue_notice(f"Needs your call: {esc['open_question']}", ref=esc["thread_id"])

    return _on


def queue_welcome(store) -> None:
    """Queue the bind greeting — and, for a seller with nothing listed yet, the first-listing CTA
    with its Skip button — stamping welcomed_at in the same transaction. Delivery is the drain
    lane's job (retried, catchup-backstopped), never a fire-and-forget send from the bind tick.
    Not holdable: the seller just scanned the QR, so this delivers at any hour."""
    entries: list = [(WELCOME_TEXT, None)]
    if not store.list_items():
        entries.append((FIRST_LISTING_CTA_TEXT, [(CTA_SKIP_LABEL, fastpaths.CB_SKIP_CTA)]))
    store.queue_welcome_notices(entries)


def first_listing_nudge(*, store, now=None) -> None:
    """One nudge, ever: bound a day, welcomed, nothing listed, Skip never tapped. Every guard
    derives from durable rows (the notice's own ref is the once-guard), so a restart can never
    re-fire it. Holdable: a proactive push waits out quiet hours. Pause gates the queueing too —
    a paused agent should not solicit work."""
    if store.is_paused():
        return
    ch = store.get_channel()
    if ch["chat_id"] is None or not ch["welcomed_at"] or not ch["bound_ts"]:
        return
    if (time.time() if now is None else now) - ch["bound_ts"] < FIRST_LISTING_NUDGE_AGE_SEC:
        return
    if store.list_items() or store.get_meta(fastpaths.META_FIRST_LISTING_SKIPPED) is not None:
        return
    if store.has_notice_with_ref(FIRST_LISTING_NUDGE_REF):
        return
    store.queue_notice(FIRST_LISTING_NUDGE_TEXT, ref=FIRST_LISTING_NUDGE_REF, holdable=True)
