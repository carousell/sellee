"""The Telegram provider's lifecycle: start its receive loop, its notice-drain lane and its typing
keeper, and shut them down.

`start` spins the poller on its own stop event, registers the notice-drain task — which uses
Telegram's `deliver` over the core outbound policy — and spins a second thread holding the chat's
typing indicator lit while the seller is waiting. The returned handle stops both threads and
removes the lane. `is_configured` is "a bot token has been written": the manager starts the provider
at boot only when that holds, and `connect` starts it at runtime otherwise.

The drain is a lane and the keeper is a thread because their clocks differ. Two seconds is a
latency target the scheduler's 5s tick merely rounds up; the indicator's refresh is a deadline the
tick cannot meet at all. `channel/presence.py` carries the full argument.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from sellee import secrets
from sellee.channel import outbound, presence
from sellee.channel.telegram import outbound as tg_outbound
from sellee.channel.telegram.poller import POLL_TIMEOUT_SEC, Poller
from sellee.scheduler import Task

log = logging.getLogger(__name__)

_DRAIN_TASK = "notice_drain"
_PRESENCE_THREAD = "channel-presence-telegram"


@dataclass
class TelegramHandle:
    stop: threading.Event
    thread: threading.Thread
    presence_thread: threading.Thread
    presence_join_sec: float
    scheduler: object
    task_names: list

    def shutdown(self) -> None:
        self.stop.set()
        for name in self.task_names:
            self.scheduler.deregister(name)
        # The poller may be mid long-poll; it exits at the next boundary. A daemon thread, so the
        # process never blocks on it beyond the join timeout.
        self.thread.join(timeout=POLL_TIMEOUT_SEC + 5)
        self.presence_thread.join(timeout=self.presence_join_sec)
        if self.presence_thread.is_alive():
            # Bounded by construction (one refresh plus a capped call), so reaching this means
            # something is wrong rather than slow. Said out loud because the lane it replaced could
            # not survive a provider switch — deregistering its name was the guarantee — while a
            # thread the manager has stopped waiting for would go on pulsing into a chat the next
            # provider now owns.
            log.warning("typing keeper outlived its join — it may still be pulsing")


def is_configured() -> bool:
    return secrets.read_telegram_bot_token() is not None


def start(*, bus, store, config, scheduler) -> TelegramHandle:
    stop = threading.Event()
    poller = Poller(store=store, config=config, bus=bus, stop_event=stop)
    scheduler.register(
        Task(
            name=_DRAIN_TASK,
            interval_sec=outbound.NOTICE_DRAIN_INTERVAL_SEC,
            func=lambda: outbound.drain_notices(
                store=store, bus=bus, deliver=tg_outbound.make_deliver(config)
            ),
        )
    )
    refresh_sec = presence.refresh_interval_sec(tg_outbound.TYPING_INDICATOR_LIFETIME_SEC)
    thread = threading.Thread(target=poller.run, name="channel-poller", daemon=True)
    thread.start()
    presence_thread = threading.Thread(
        target=lambda: presence.keep_typing(
            store=store,
            bus=bus,
            typing=tg_outbound.make_typing(config, timeout=presence.TYPING_CALL_TIMEOUT_SEC),
            refresh_sec=refresh_sec,
            stop=stop,
        ),
        name=_PRESENCE_THREAD,
        daemon=True,
    )
    presence_thread.start()
    return TelegramHandle(
        stop=stop,
        thread=thread,
        presence_thread=presence_thread,
        presence_join_sec=refresh_sec
        + presence.TYPING_CALL_TIMEOUT_SEC
        + presence.PRESENCE_JOIN_GRACE_SEC,
        scheduler=scheduler,
        task_names=[_DRAIN_TASK],
    )
