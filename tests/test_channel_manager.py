"""The channel manager (register/deregister/shutdown_all + register_configured) and the Telegram
provider's start/is_configured/shutdown. A provider runs only when registered — so a daemon with
no channel configured starts nothing.
"""

from __future__ import annotations

import threading

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from sellee import secrets
from sellee.channel.manager import ChannelManager
from sellee.channel.telegram import provider as telegram_provider
from sellee.config import Config
from sellee.scheduler import Scheduler

# Every wait in the concurrency tests below is a backstop against a wedge, never a deadline an
# assertion depends on — so it is set well past anything a loaded CI box could plausibly need.
_DEADLOCK_BACKSTOP_SEC = 30.0


class _FakeProvider:
    """A provider stub: records start/shutdown and whether it is 'configured'."""

    def __init__(self, configured: bool):
        self._configured = configured
        self.started = 0
        self.handle = _FakeHandle()

    def is_configured(self) -> bool:
        return self._configured

    def start(self, **deps):
        self.started += 1
        return self.handle


class _FakeHandle:
    def __init__(self):
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


def _manager(providers, bus):
    return ChannelManager(providers=providers, bus=bus, store=None, config=Config(), scheduler=None)


# --- manager register/deregister ------------------------------------------------------------


def test_register_is_idempotent(bus) -> None:
    p = _FakeProvider(configured=True)
    mgr = _manager({"telegram": p}, bus)
    mgr.register("telegram")
    mgr.register("telegram")  # already running -> no-op
    assert p.started == 1


def test_deregister_shuts_down_and_allows_restart(bus) -> None:
    p = _FakeProvider(configured=True)
    mgr = _manager({"telegram": p}, bus)
    mgr.register("telegram")
    mgr.deregister("telegram")
    assert p.handle.shutdowns == 1
    mgr.deregister("telegram")  # not running -> no-op
    assert p.handle.shutdowns == 1
    mgr.register("telegram")  # can start again after deregister
    assert p.started == 2


def test_register_configured_starts_only_configured(bus) -> None:
    on, off = _FakeProvider(configured=True), _FakeProvider(configured=False)
    mgr = _manager({"on": on, "off": off}, bus)
    mgr.register_configured()
    assert on.started == 1 and off.started == 0


# --- only one provider runs at a time (the channel row is a singleton bound to one adapter) --


def test_register_deregisters_any_other_running_provider(bus) -> None:
    """Both providers register scheduler tasks under identical literal names (notice_drain /
    typing_pulse), so switching providers has to tear the old one down: a dangling handle keeps
    its scheduler lanes, and a later reconnect back to the first provider would be a no-op that
    never reclaims them — silently routing its notices through the other provider's transport."""
    telegram = _FakeProvider(configured=True)
    discord = _FakeProvider(configured=True)
    mgr = _manager({"telegram": telegram, "discord": discord}, bus)

    mgr.register("telegram")
    assert telegram.started == 1
    assert telegram.handle.shutdowns == 0

    mgr.register("discord")  # switching providers must tear the old one down
    assert discord.started == 1
    assert telegram.handle.shutdowns == 1  # telegram was deregistered, not left dangling

    mgr.register("telegram")  # reconnecting telegram must not be a no-op: discord is running
    assert telegram.started == 2  # started fresh, not skipped as "already running"
    assert discord.handle.shutdowns == 1


def test_concurrent_registers_of_the_same_provider_start_it_once(bus) -> None:
    """Two connect calls can land at once — the control server is a ThreadingHTTPServer. Because
    `register` holds `_register_lock` across the whole deregister-then-start sequence, the second
    caller cannot run concurrently at all: it queues, then finds the handle already there and
    no-ops. Without that it would start a second provider on top of the winner's — a live gateway
    thread nothing will ever shut down, holding the scheduler task names the survivor believes it
    owns.

    Made deterministic: the first caller is parked inside the sibling's `shutdown()`, holding
    `_register_lock`, until the test releases it."""
    entered_teardown = threading.Event()
    release_teardown = threading.Event()

    class _BlockingHandle(_FakeHandle):
        def shutdown(self) -> None:
            entered_teardown.set()
            release_teardown.wait(timeout=_DEADLOCK_BACKSTOP_SEC)
            super().shutdown()

    telegram = _FakeProvider(configured=True)
    telegram.handle = _BlockingHandle()
    discord = _FakeProvider(configured=True)
    mgr = _manager({"telegram": telegram, "discord": discord}, bus)
    mgr.register("telegram")

    first = threading.Thread(target=mgr.register, args=("discord",), daemon=True)
    first.start()
    assert entered_teardown.wait(timeout=_DEADLOCK_BACKSTOP_SEC)

    second = threading.Thread(target=mgr.register, args=("discord",), daemon=True)
    second.start()
    second.join(timeout=0.5)
    assert second.is_alive()  # queued behind the first caller, not racing it
    assert discord.started == 0

    release_teardown.set()
    first.join(timeout=_DEADLOCK_BACKSTOP_SEC)
    second.join(timeout=_DEADLOCK_BACKSTOP_SEC)
    assert discord.started == 1  # the queued caller found the handle and no-oped
    assert mgr._handles["discord"] is discord.handle


def test_concurrent_registers_of_different_providers_start_only_one(bus) -> None:
    """The same-provider race above has a cross-provider twin: from a state where telegram is
    already running, two connect calls for two DIFFERENT other providers can both compute the same
    non-empty `others = ["telegram"]` before either has finished tearing it down. `deregister` pops
    with a default, so only one of the two actually removes telegram's handle and calls shutdown —
    the other's `deregister` call is a silent no-op. Without a lock spanning the whole method, that
    second caller then walks straight into its own final block against an untouched `_handles` and
    starts its own provider on top of the winner's: both end up running, silently colliding on the
    scheduler task names they share. `register` now holds `_register_lock` for the entire
    deregister-then-start sequence, so the second caller cannot even begin its own attempt until
    the first has completely finished — by which point it sees the first provider running and
    tears it down before starting its own, same as any other provider switch.

    Made deterministic: the winner of the deregister race is parked inside telegram's `shutdown()`
    — after it has already popped the handle out of `_handles`, the exact state a second caller
    would otherwise race in against."""
    entered_teardown = threading.Event()
    release_teardown = threading.Event()

    class _BlockingHandle(_FakeHandle):
        def shutdown(self) -> None:
            entered_teardown.set()
            release_teardown.wait(timeout=_DEADLOCK_BACKSTOP_SEC)
            super().shutdown()

    telegram = _FakeProvider(configured=True)
    telegram.handle = _BlockingHandle()
    discord = _FakeProvider(configured=True)
    whatsapp = _FakeProvider(configured=True)
    mgr = _manager({"telegram": telegram, "discord": discord, "whatsapp": whatsapp}, bus)
    mgr.register("telegram")

    first = threading.Thread(target=mgr.register, args=("discord",), daemon=True)
    first.start()
    # telegram's handle is already popped from _handles by the time teardown is entered
    assert entered_teardown.wait(timeout=_DEADLOCK_BACKSTOP_SEC)

    second = threading.Thread(target=mgr.register, args=("whatsapp",), daemon=True)
    second.start()

    release_teardown.set()
    first.join(timeout=_DEADLOCK_BACKSTOP_SEC)
    second.join(timeout=_DEADLOCK_BACKSTOP_SEC)

    assert discord.started == 1
    assert whatsapp.started == 1
    assert discord.handle.shutdowns == 1  # torn down once whatsapp registered, not left dangling
    assert mgr._handles == {"whatsapp": whatsapp.handle}


class _FakeStore:
    def __init__(self, adapter: str):
        self._adapter = adapter

    def get_channel(self):
        return {"adapter": self._adapter}


def test_register_configured_prefers_the_bound_adapter_when_multiple_are_configured(bus) -> None:
    """A seller who tried both providers at different times can leave both token files behind
    (`is_configured()` true for both) even though only one was ever actually bound. Boot must
    start only the one the channel row's `adapter` says is bound, not blindly start every
    configured provider — dict iteration order, not the seller's actual bound provider, would
    otherwise decide which one wins (via `register`'s own mutual-exclusion teardown)."""
    telegram = _FakeProvider(configured=True)
    discord = _FakeProvider(configured=True)
    mgr = ChannelManager(
        providers={"telegram": telegram, "discord": discord},
        bus=bus,
        store=_FakeStore(adapter="discord"),
        config=Config(),
        scheduler=None,
    )
    mgr.register_configured()
    assert discord.started == 1
    assert telegram.started == 0


def test_shutdown_all_stops_running(bus) -> None:
    p = _FakeProvider(configured=True)
    mgr = _manager({"telegram": p}, bus)
    mgr.register("telegram")
    mgr.shutdown_all()
    assert p.handle.shutdowns == 1


# --- the real Telegram provider -------------------------------------------------------------


def test_is_configured_reflects_the_token(store, bus, xdg_tmp) -> None:
    assert telegram_provider.is_configured() is False
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    assert telegram_provider.is_configured() is True


def test_start_spins_poller_and_registers_lanes_then_shuts_down(store, bus, xdg_tmp) -> None:
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1, nonce=store.get_channel()["bind_nonce"])
    scheduler = Scheduler(bus, tick_interval_sec=60, stop_event=threading.Event())
    with FakeTelegramAPI():
        handle = telegram_provider.start(store=store, bus=bus, config=Config(), scheduler=scheduler)
        try:
            assert handle.thread.is_alive()
            # the delivery lanes are registered while the provider runs
            assert "notice_drain" in scheduler._reg.tasks
            assert "typing_pulse" in scheduler._reg.tasks
        finally:
            handle.shutdown()
        assert not handle.thread.is_alive()  # joined
        assert "notice_drain" not in scheduler._reg.tasks  # lanes removed on shutdown
        assert "typing_pulse" not in scheduler._reg.tasks


def test_scheduler_deregister_stops_scheduling(bus) -> None:
    from sellee.scheduler import Task

    ran = []
    sched = Scheduler(bus, tick_interval_sec=60, stop_event=threading.Event())
    sched.register(Task(name="t", interval_sec=0, func=lambda: ran.append(1)))
    sched.deregister("t")
    sched.run_once()
    assert ran == []  # a deregistered task is never claimed
