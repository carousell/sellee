"""Showing the seller the wait: the typing keeper's cadence and health, the gate deciding when the
chat should be lit, and the one thing said out loud when a wait outruns what an indicator can carry.

The indicator used to be a secondary signal sitting beside a sentence that narrated every arrival.
It is now the only thing showing an ordinary wait, so its two old tolerances — a blink once a cycle,
and failures swallowed where nothing could see them — are defects rather than trade-offs.
"""

from __future__ import annotations

import threading

import pytest

from fake_telegram_api import CHAT_ID, FAKE_TOKEN
from sellee import secrets
from sellee.channel import outbound, presence
from sellee.channel.discord import outbound as discord_outbound
from sellee.channel.telegram import outbound as tg_outbound


def _bound(store):
    """Bind the channel for real, which writes a bot token — so every caller must take `xdg_tmp`.

    Not optional and not cosmetic: without it `write_telegram_bot_token` resolves against the real
    XDG config dir and overwrites the developer's own live token with FAKE_TOKEN, which is a 401
    from Telegram until they fetch it back from BotFather. This happened.
    """
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1, nonce=store.get_channel()["bind_nonce"])


def _ev(uid, kind="text", text="hi", **payload):
    return {"event_id": uid, "kind": kind, "text": text, "payload": payload, "src_ts": 1.0}


def _waiting(store, uid=1):
    """A seller message routed to a channel pass: the state the indicator exists for."""
    store.ingest_updates([_ev(uid)], update_offset=uid + 1)
    return store.enqueue_channel_pass()


class _Recorder:
    """A typing mechanism that records, and optionally fails."""

    def __init__(self, fail_on=()):
        self.calls: list = []
        self.fail_on = set(fail_on)

    def __call__(self, chat_id) -> None:
        self.calls.append(chat_id)
        if len(self.calls) in self.fail_on:
            raise RuntimeError("bot api unreachable")


# --- the cadence ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lifetime", "expected"),
    [
        (tg_outbound.TYPING_INDICATOR_LIFETIME_SEC, 4.0),
        (discord_outbound.TYPING_INDICATOR_LIFETIME_SEC, 9.0),
    ],
    ids=["telegram-5s", "discord-10s"],
)
def test_the_refresh_lands_before_the_indicator_expires(lifetime, expected) -> None:
    """The defect this whole mechanism exists for: the old lane asked for 4.0s, the scheduler's
    flat 5.0s tick gave it ~5.14s, and Telegram's indicator lives ~5s — so it blinked off once per
    cycle while a comment claimed it was held open."""
    refresh = presence.refresh_interval_sec(lifetime)

    assert refresh == expected
    assert refresh < lifetime


def test_the_refresh_is_derived_from_the_platform_not_from_the_tick() -> None:
    """`refresh < lifetime` holds by construction rather than by configuration, so no tick change
    and no config edit can silently reopen the blink."""
    for lifetime in (1.0, 2.5, 5.0, 10.0, 60.0):
        assert presence.refresh_interval_sec(lifetime) < lifetime


def test_an_implausibly_short_lifetime_never_becomes_a_hot_loop() -> None:
    """The floor yields to the guarantee rather than breaking it: a refresh longer than the lifetime
    would be defending someone's API by making the indicator useless."""
    assert 0 < presence.refresh_interval_sec(0.2) < 0.2
    assert presence.refresh_interval_sec(30.0) > presence.TYPING_MIN_REFRESH_SEC


def test_the_call_is_bounded_well_inside_the_refresh() -> None:
    """A pulse still in flight when the indicator expires has already missed its only chance to be
    useful, and waiting on it delays the retry that would fix it."""
    telegram_refresh = presence.refresh_interval_sec(tg_outbound.TYPING_INDICATOR_LIFETIME_SEC)

    assert presence.TYPING_CALL_TIMEOUT_SEC < telegram_refresh


# --- the gate ------------------------------------------------------------------------------------


def test_the_chat_is_lit_while_the_seller_is_owed_an_answer(store, xdg_tmp) -> None:
    _bound(store)
    _waiting(store)

    assert outbound.typing_target(store) == CHAT_ID


def test_nothing_is_lit_with_no_pass_in_flight(store, xdg_tmp) -> None:
    _bound(store)

    assert outbound.typing_target(store) is None


def test_nothing_is_lit_while_paused(store, xdg_tmp) -> None:
    """Nothing runs while paused — the pass lane claims nothing and a running pass is killed — so an
    indicator would be the agent miming work it is forbidden to do. `channel/acks.py` says it in
    words instead, which is the only signal available here."""
    _bound(store)
    _waiting(store)
    store.set_paused(True, source="test")

    assert outbound.typing_target(store) is None


def test_nothing_is_lit_while_unbound(store) -> None:
    _waiting(store)

    assert outbound.typing_target(store) is None


def test_the_indicator_stops_once_the_pass_has_spoken(store, xdg_tmp) -> None:
    """`send_message` only queues; the drain delivers a moment later and clears the indicator
    itself. Without this the keeper re-arms behind an answer already on screen, and a trailing
    "typing…" reads as "there's more coming"."""
    _bound(store)
    pass_id = _waiting(store)
    assert outbound.typing_target(store) == CHAT_ID

    store.queue_notice("Yes — S$35, still listed.", pass_id=pass_id)

    assert outbound.typing_target(store) is None


def test_the_indicator_goes_dark_past_the_lit_bound(store, xdg_tmp) -> None:
    """A pass killed outright keeps its `running` row until the stale sweep, which is a quarter of
    an hour away. Unbounded, the keeper would animate "typing…" with no process behind it."""
    _bound(store)
    _waiting(store)
    active = store.active_channel_pass()
    past = active["requested_ts"] + outbound.TYPING_MAX_LIT_SEC + 1

    assert outbound.typing_target(store, now=past) is None
    assert outbound.typing_target(store, now=active["requested_ts"] + 1) == CHAT_ID


def test_a_queued_pass_counts_as_waiting(store, xdg_tmp) -> None:
    """`started_ts` is None while a pass waits its turn on the lane, and that wait is the seller's
    wait too — so the bound is measured from when it was requested."""
    _bound(store)
    _waiting(store)

    assert store.active_channel_pass()["started_ts"] is None
    assert outbound.typing_target(store) == CHAT_ID


# --- the keeper ----------------------------------------------------------------------------------


def _run_cycles(store, bus, typing, n, refresh_sec=0.0):
    """Drive the keeper for exactly `n` cycles, off its own injected clock.

    `keep_typing` reads the clock twice per iteration — once to fix the deadline before the call,
    once to work out what is left of it — so the 2n-th read is the last one iteration n makes, and
    stopping there leaves the `while` to end the loop before iteration n+1 pulses.
    """
    stop = threading.Event()
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        if ticks["n"] >= n * 2:
            stop.set()
        return 0.0

    presence.keep_typing(
        store=store, bus=bus, typing=typing, refresh_sec=refresh_sec, stop=stop, clock=clock
    )


def test_the_keeper_lights_the_chat_every_cycle_while_the_seller_waits(store, bus, xdg_tmp) -> None:
    _bound(store)
    _waiting(store)
    typing = _Recorder()

    _run_cycles(store, bus, typing, n=3)

    assert typing.calls == [CHAT_ID, CHAT_ID, CHAT_ID]


def test_the_keeper_is_silent_when_the_gate_is_shut(store, bus, xdg_tmp) -> None:
    _bound(store)
    typing = _Recorder()

    _run_cycles(store, bus, typing, n=3)

    assert typing.calls == []


def test_a_pulse_failure_is_reported_once_not_every_cycle(store, bus, xdg_tmp) -> None:
    """The old lane could not report these at all: the pulse swallowed every exception at DEBUG, so
    the scheduler saw task.ok forever and a revoked token was invisible. A signal firing every few
    seconds must report the transition, not the state, or it buries the moment it broke."""
    _bound(store)
    _waiting(store)
    typing = _Recorder(fail_on=(1, 2, 3))

    _run_cycles(store, bus, typing, n=3)

    assert len(bus.store.read(kinds=["channel.presence.stalled"])) == 1


def test_recovery_is_reported_once(store, bus, xdg_tmp) -> None:
    _bound(store)
    _waiting(store)
    typing = _Recorder(fail_on=(1,))

    _run_cycles(store, bus, typing, n=3)

    assert len(bus.store.read(kinds=["channel.presence.stalled"])) == 1
    assert len(bus.store.read(kinds=["channel.presence.resumed"])) == 1


def test_a_wait_that_ends_is_not_mistaken_for_a_recovery(store, bus, xdg_tmp) -> None:
    """Gated is not unhealthy — there is simply nothing to say — so the health state survives a
    quiet stretch and the next real failure is still a transition."""
    _bound(store)
    typing = _Recorder()

    _run_cycles(store, bus, typing, n=3)

    assert bus.store.read(kinds=["channel.presence.resumed"]) == []


def test_a_store_fault_does_not_kill_the_keeper(store, bus, monkeypatch, xdg_tmp) -> None:
    """Everything here reads SQLite, shared with the scheduler pool, the receive loop and the
    passes. A locked database must cost a cycle, never the thread: there is no lane underneath to
    notice it died and nothing that would restart it."""
    _bound(store)
    _waiting(store)
    typing = _Recorder()
    calls = {"n": 0}
    real = store.active_channel_pass

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real()

    monkeypatch.setattr(store, "active_channel_pass", flaky)

    _run_cycles(store, bus, typing, n=3)

    assert typing.calls == [CHAT_ID, CHAT_ID]  # cycle 1 lost, the thread survived it


def test_the_keeper_stops_when_the_stop_event_is_set(store, bus, xdg_tmp) -> None:
    _bound(store)
    _waiting(store)
    typing = _Recorder()
    stop = threading.Event()
    stop.set()

    presence.keep_typing(store=store, bus=bus, typing=typing, refresh_sec=0.0, stop=stop)

    assert typing.calls == []


# --- the threshold notice ------------------------------------------------------------------------


def _waited(store, seconds):
    """`now` for a seller who has been waiting `seconds`."""
    return store.oldest_unanswered_message()["received_ts"] + seconds


def test_an_ordinary_wait_is_never_named(store, xdg_tmp) -> None:
    """Measured passes run ~80s at the median and ~155s at p75. That silence is the entire point of
    showing the wait rather than narrating it — only the tail earns words."""
    _bound(store)
    _waiting(store)

    presence.seller_waiting_notice(store=store, now=_waited(store, 155))

    assert store.list_queued_notices() == []


def test_an_abnormal_wait_is_named_once(store, xdg_tmp) -> None:
    _bound(store)
    _waiting(store)
    now = _waited(store, presence.SELLER_WAITING_AGE_SEC + 20)

    presence.seller_waiting_notice(store=store, now=now)
    presence.seller_waiting_notice(store=store, now=now + 60)

    assert len(store.list_queued_notices()) == 1


def test_the_waiting_notice_carries_no_pass_id(store, xdg_tmp) -> None:
    """Load-bearing rather than incidental. `typing_target` darkens the indicator once the waiting
    pass has spoken and `_channel_progressed` reads the same mark — so attributing this notice to
    the pass would put the indicator out in the middle of the one wait long enough to need it, and
    file a pass that never said a word as a success."""
    _bound(store)
    pass_id = _waiting(store)

    presence.seller_waiting_notice(
        store=store, now=_waited(store, presence.SELLER_WAITING_AGE_SEC + 20)
    )

    assert len(store.list_queued_notices()) == 1
    assert not store.has_notice_for_pass(pass_id)
    assert outbound.typing_target(store) == CHAT_ID  # still lit, which is the whole point


def test_a_fresh_wait_after_an_answer_is_named_afresh(store, xdg_tmp) -> None:
    """Guarded by the message, not the conversation: a seller who is answered and then left waiting
    again is a second wait, not the first one repeating."""
    _bound(store)
    first = _waiting(store, uid=1)
    presence.seller_waiting_notice(
        store=store, now=_waited(store, presence.SELLER_WAITING_AGE_SEC + 20)
    )
    assert len(store.list_queued_notices()) == 1

    # The first wait ends: its pass answers and its rows settle out of the unanswered read.
    store.queue_notice("here you go", pass_id=first)
    store.fold_settled_inbox("failed")
    store.mark_inbox_handled([row["id"] for row in store.inbox_for_pass(first)], "answered")

    _waiting(store, uid=2)
    presence.seller_waiting_notice(
        store=store, now=_waited(store, presence.SELLER_WAITING_AGE_SEC + 20)
    )

    # Three: the first wait's notice, the answer, and the second wait named on its own account.
    assert len([n for n in store.list_queued_notices() if n["ref"]]) == 2


def test_nothing_is_said_when_no_pass_is_running(store, xdg_tmp) -> None:
    """A row can sit unsettled with no pass behind it — held by a pause that has since lifted, or
    between the fold and the route lane's next tick. "Still working on it" would be a lie."""
    _bound(store)
    store.ingest_updates([_ev(1)], update_offset=2)

    presence.seller_waiting_notice(
        store=store, now=_waited(store, presence.SELLER_WAITING_AGE_SEC + 20)
    )

    assert store.list_queued_notices() == []


def test_a_paused_agent_says_nothing_on_this_lane(store, xdg_tmp) -> None:
    _bound(store)
    _waiting(store)
    store.set_paused(True, source="test")

    presence.seller_waiting_notice(
        store=store, now=_waited(store, presence.SELLER_WAITING_AGE_SEC + 20)
    )

    assert store.list_queued_notices() == []


def test_an_unbound_channel_says_nothing_on_this_lane(store) -> None:
    _waiting(store)

    presence.seller_waiting_notice(
        store=store, now=_waited(store, presence.SELLER_WAITING_AGE_SEC + 20)
    )

    assert store.list_queued_notices() == []


def test_the_waiting_line_never_implies_speed(store) -> None:
    for implies_speed in ("one sec", "a moment", "one moment", "shortly", "right away"):
        assert implies_speed not in presence.SELLER_WAITING_TEXT.lower()
