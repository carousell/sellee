"""The Telegram provider's lifecycle: start its receive loop + delivery lanes, and shut them down.

`start` spins the poller on its own stop event and registers the provider-specific delivery tasks
(notice drain, typing pulse) — which use Telegram's `deliver`/`typing` mechanisms over the core
outbound policy — so those lanes exist only while the provider runs. The returned handle stops the
poller and removes those lanes. `is_configured` is "a bot token has been written" — the manager
starts the provider at boot only when that holds, and `connect` starts it at runtime otherwise.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from selly_agent import secrets
from selly_agent.channel import outbound
from selly_agent.channel.telegram import outbound as tg_outbound
from selly_agent.channel.telegram.poller import POLL_TIMEOUT_SEC, Poller
from selly_agent.scheduler import Task

_DRAIN_TASK = "notice_drain"
_TYPING_TASK = "typing_pulse"


@dataclass
class TelegramHandle:
    stop: threading.Event
    thread: threading.Thread
    scheduler: object
    task_names: list

    def shutdown(self) -> None:
        self.stop.set()
        for name in self.task_names:
            self.scheduler.deregister(name)
        # The poller may be mid long-poll; it exits at the next boundary. A daemon thread, so the
        # process never blocks on it beyond the join timeout.
        self.thread.join(timeout=POLL_TIMEOUT_SEC + 5)


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
    scheduler.register(
        Task(
            name=_TYPING_TASK,
            interval_sec=outbound.TYPING_PULSE_INTERVAL_SEC,
            func=lambda: outbound.pulse_typing(store=store, typing=tg_outbound.make_typing(config)),
        )
    )
    thread = threading.Thread(target=poller.run, name="channel-poller", daemon=True)
    thread.start()
    return TelegramHandle(
        stop=stop, thread=thread, scheduler=scheduler, task_names=[_DRAIN_TASK, _TYPING_TASK]
    )
