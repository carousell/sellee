"""Showing the seller the wait instead of narrating it: the live typing indicator's cadence, and
the one thing said out loud when a wait stops being ordinary.

The chat used to be told "I'm working out what to do — that usually takes a minute or two,
sometimes longer" on every single arrival. It was honest and it was identical every time, which is
what made it read as a machine. The wait itself is better shown than described: every platform this
agent speaks on has a typing indicator, and an indicator is *continuously* true in a way a sentence
written at t=0 can never be.

Two mechanisms, in ascending cost:

  * **The keeper** — a thread per provider holding the indicator lit for as long as the seller is
    owed an answer. Costs one API call per refresh and says nothing.
  * **The threshold notice** — real words, once, when a wait outruns what an indicator can honestly
    carry. Costs a message, so it fires on the minority of passes that earn it.

## Why a thread and not a scheduler lane

Arithmetic, not taste. `Scheduler.run()` is `tick(); stop.wait(tick_interval)` — a flat sleep, not
a sleep-until-next-due — so a lane's real period is the tick (5.0s by default), whatever interval
it declares; `Scheduler.register` logs a warning saying so, and named the old `typing_pulse` lane in
it. Against an indicator that lives ~5s on Telegram, a 5.0s+ pulse blinks off once per cycle. That
was tolerable while the indicator was a secondary signal sitting beside a receipt. It is not
tolerable now that it is the only signal, and no interval below the tick can fix it without moving
the tick, which six other lanes share.

The pool is the second reason. `pass_lane` occupies one of `MAX_WORKERS` (4) executor threads
blocking on `proc.wait()` for as long as the pass runs, and the channel lanes register last — so
the indicator could be starved by exactly the pass it exists to cover.

A thread costs the lane's ledger (`task.start`/`ok`/`error`) and its backoff. Both are replaced
here deliberately: the ledger by transition events, which are what an operator actually wants from
a signal that fires every few seconds, and the backoff by nothing — a failing pulse should retry at
its own cadence, not decay to five minutes, because it is a liveness signal and a decayed one is a
dead one.

What the thread must reproduce from the lane, and does, by deriving everything from durable rows on
every cycle: a pass enqueued by the `channel_route` lane rather than by an ingest, a pass that was
queued behind another, and a restart in the middle of any of it.

`Task.jitter`'s doctrine — never a perfectly regular interval on anything an outsider can watch —
does not apply. It exists for marketplace reads, where inter-arrival regularity is what an
account-integrity model notices. This traffic goes to a chat platform the agent is a declared bot
on, over an endpoint whose entire purpose is to be called on a timer.
"""

from __future__ import annotations

import logging
import time

from sellee.channel import outbound

log = logging.getLogger(__name__)

# How far ahead of expiry to refresh. Absolute rather than a fraction of the lifetime, because what
# it has to cover is one API round trip, and a round trip does not get longer because a platform
# holds its indicator for ten seconds instead of five.
TYPING_REFRESH_MARGIN_SEC = 1.0
# A floor, so a platform that ever reported an implausibly short lifetime could not turn this into
# a hot loop against its own API.
TYPING_MIN_REFRESH_SEC = 1.0
# What one pulse may cost before it is abandoned. Strictly inside the refresh interval, which is
# strictly inside the shortest lifetime: a call still in flight when the indicator expires has
# already failed at its only job, and waiting on it delays the retry that would fix it. The
# transports default to 60s, which is right for delivering a message and absurd for this.
TYPING_CALL_TIMEOUT_SEC = 3.0
# How long to wait for the thread at shutdown: one refresh, plus the longest a call in flight can
# take, plus a little.
PRESENCE_JOIN_GRACE_SEC = 1.0

# What the seller hears when a wait stops being ordinary.
#
# Deliberately the same shape as `outbound.BUYER_WAITING_TEXT`: it names what is being waited on and
# it does not guess at an outcome. It never implies speed — the house rule in voice-and-style.md —
# and it never repeats, because the indicator is doing the repeating and this is the exception.
SELLER_WAITING_TEXT = (
    "Still working on this one — it's taking longer than usual. I'll come back to you here as soon "
    "as I have something."
)
SELLER_WAITING_REF = "seller-waiting"
# Measured channel passes run 31.6s at the fastest, ~80s median, ~155s at p75, with a tail past
# five minutes. 180s sits just past p75, so an ordinary wait is never named — that silence is the
# entire point of showing the wait rather than narrating it — and only the tail earns words.
SELLER_WAITING_AGE_SEC = 180.0
SELLER_WAITING_INTERVAL_SEC = 60.0


def refresh_interval_sec(lifetime_sec: float) -> float:
    """How often to re-light an indicator that lives `lifetime_sec`.

    `refresh < lifetime` holds for every input, by construction rather than by configuration —
    unlike the old lane, whose 4.0s interval was silently floored at the 5.0s tick and so was wrong
    by a fraction of a second that no test could see.

    The floor yields rather than break that. It is there to stop an implausibly short lifetime
    turning this into a hot loop against someone's API, but a floor that could exceed the lifetime
    would be defending the API by making the indicator useless — so below 2s it halves the lifetime
    instead. No platform is anywhere near that; it costs one `min` to make the guarantee hold
    without a caveat.
    """
    floor = min(TYPING_MIN_REFRESH_SEC, lifetime_sec / 2)
    return max(lifetime_sec - TYPING_REFRESH_MARGIN_SEC, floor)


def keep_typing(*, store, bus, typing, refresh_sec: float, stop, clock=time.monotonic) -> None:
    """Hold the chat's typing indicator lit for as long as the seller is owed an answer.

    Runs on its own thread until `stop` is set. `typing(chat_id)` is the provider's mechanism;
    `refresh_sec` comes from that provider's indicator lifetime. The decision of *whether* to light
    is `outbound.typing_target`, shared with the inline pulse at ingest.

    The interval is measured from before the call rather than after it, so a slow-but-successful
    pulse eats into its own gap instead of pushing the next one past the indicator's expiry. A call
    slow enough to blink anyway has already published `stalled`.

    Failures are reported on transition only — first failure after a success, and recovery — because
    a signal that fires every few seconds would otherwise fill the event log with one line per
    cycle and bury the moment it actually broke. The old lane could not report them at all: the
    pulse swallowed every exception at DEBUG, so the scheduler saw `task.ok` forever and a revoked
    token or a blocked bot was completely invisible.
    """
    healthy = True
    while not stop.is_set():
        next_at = clock() + refresh_sec
        try:
            healthy = _pulse_once(store=store, bus=bus, typing=typing, healthy=healthy)
        except Exception:
            # Blanket, and not only around the send: everything here reads SQLite, and the database
            # is shared with the scheduler pool, the receive loop and the passes. A locked database
            # must cost this thread a cycle, never its life — there is no lane underneath to notice
            # it died and no supervisor that would restart it. `Poller.run` guards itself the same
            # way, for the same reason.
            log.exception("typing keeper cycle failed (continuing)")
        stop.wait(max(0.0, next_at - clock()))


def _pulse_once(*, store, bus, typing, healthy: bool) -> bool:
    """One cycle: light the chat if it should be lit. Returns the new health state.

    Gated is not unhealthy — there is simply nothing to say — so a wait that ends leaves the health
    state exactly as it found it, and the next real failure is still reported as a transition.
    """
    chat_id = outbound.typing_target(store)
    if chat_id is None:
        return healthy
    try:
        typing(chat_id)
    except Exception as exc:
        if healthy:
            log.warning("typing indicator stalled: %s", exc)
            bus.publish("channel.presence.stalled", {"error": repr(exc)})
        return False
    if not healthy:
        log.info("typing indicator recovered")
        bus.publish("channel.presence.resumed", {})
    return True


def seller_waiting_notice(*, store, now=None) -> None:
    """Say something, once, when the seller has been waiting longer than an indicator can carry.

    The indicator is a header subtitle: no bubble, no notification, no scrollback. For an ordinary
    pass that is the right weight — it shows the wait without adding to what the seller has to read.
    For a wait in the tail it is not enough, and a seller who tapped a button and locked their phone
    has been told nothing at all. This is the durable half: a real message, a real push, and a row
    that outlives any of it.

    Modelled on `outbound.buyer_waiting_notice`, which solved the same problem from the other side —
    including its guard. The `ref` is keyed to the *message*, not the conversation, so a seller who
    is answered and then left waiting again is reported afresh instead of being written off by the
    first notice. Not holdable: a notice about a wait that arrives after quiet hours arrives exactly
    when it stopped mattering.

    Queued with no `pass_id`, deliberately, and this is load-bearing rather than incidental.
    `typing_target` darkens the indicator once the waiting pass has spoken, and
    `_channel_progressed`
    reads the same mark to decide whether the pass did its job. Attributing this notice to the pass
    would turn the agent's own "still working" into the pass having answered — putting the indicator
    out at 180s, in the middle of the one wait long enough to need it, and filing a pass that never
    said a word as a success.
    """
    if store.is_paused():
        return
    if store.get_channel()["chat_id"] is None:
        return
    # Never "I'm still on it" when nothing is: a row can sit unsettled with no pass behind it — held
    # by a pause that has since lifted, or between the fold and the route lane's next tick.
    if not store.has_active_channel_pass():
        return
    waiting = store.oldest_unanswered_message()
    if waiting is None:
        return
    now = time.time() if now is None else now
    if now - waiting["received_ts"] < SELLER_WAITING_AGE_SEC:
        return
    ref = f"{SELLER_WAITING_REF}:{waiting['id']}"
    if store.has_notice_with_ref(ref):
        return
    store.queue_notice(SELLER_WAITING_TEXT, ref=ref)
