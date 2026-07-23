"""The channel manager (register/deregister/shutdown_all + register_configured) and the Telegram
provider's start/is_configured/shutdown. A provider runs only when registered — so a daemon with
no channel configured starts nothing.
"""

from __future__ import annotations

import threading

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from selly_agent import secrets
from selly_agent.channel.manager import ChannelManager
from selly_agent.channel.telegram import provider as telegram_provider
from selly_agent.config import Config
from selly_agent.scheduler import Scheduler


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
    store.arm_bind("selly_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1)
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
    from selly_agent.scheduler import Task

    ran = []
    sched = Scheduler(bus, tick_interval_sec=60, stop_event=threading.Event())
    sched.register(Task(name="t", interval_sec=0, func=lambda: ran.append(1)))
    sched.deregister("t")
    sched.run_once()
    assert ran == []  # a deregistered task is never claimed
