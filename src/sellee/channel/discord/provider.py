"""The Discord provider's lifecycle: start its Gateway session thread, its notice-drain lane and its
typing keeper, and shut them down.

Mirrors telegram/provider.py exactly at this seam — `start` spins the Gateway on its own stop event,
registers the notice-drain task over the core outbound policy, and spins a second thread holding the
chat's typing indicator lit while the seller is waiting; the returned handle stops both threads and
removes the lane. `is_configured` is "a bot token has been written."

The one place the two providers legitimately differ is the refresh cadence, and it is derived rather
than written: Discord holds its indicator for 10 seconds against Telegram's 5, so the same
`presence.refresh_interval_sec` reads each platform's own constant and returns a different number.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from sellee import secrets
from sellee.channel import outbound, presence
from sellee.channel.discord import outbound as discord_outbound
from sellee.channel.discord.gateway import DiscordGateway
from sellee.scheduler import Task

log = logging.getLogger(__name__)

_DRAIN_TASK = "notice_drain"
_PRESENCE_THREAD = "channel-presence-discord"


@dataclass
class DiscordHandle:
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
        self.thread.join(timeout=10.0)
        self.presence_thread.join(timeout=self.presence_join_sec)
        if self.presence_thread.is_alive():
            # See telegram/provider.py: bounded by construction, so reaching this is a fault rather
            # than slowness, and a keeper the manager stopped waiting for would pulse into a chat
            # the next provider now owns.
            log.warning("typing keeper outlived its join — it may still be pulsing")


def is_configured() -> bool:
    return secrets.read_discord_bot_token() is not None


def start(*, bus, store, config, scheduler) -> DiscordHandle:
    stop = threading.Event()
    gateway = DiscordGateway(store=store, config=config, bus=bus, stop_event=stop)
    scheduler.register(
        Task(
            name=_DRAIN_TASK,
            interval_sec=outbound.NOTICE_DRAIN_INTERVAL_SEC,
            func=lambda: outbound.drain_notices(
                store=store, bus=bus, deliver=discord_outbound.make_deliver(config)
            ),
        )
    )
    refresh_sec = presence.refresh_interval_sec(discord_outbound.TYPING_INDICATOR_LIFETIME_SEC)
    thread = threading.Thread(target=gateway.run, name="channel-discord-gateway", daemon=True)
    thread.start()
    presence_thread = threading.Thread(
        target=lambda: presence.keep_typing(
            store=store,
            bus=bus,
            typing=discord_outbound.make_typing(config, timeout=presence.TYPING_CALL_TIMEOUT_SEC),
            refresh_sec=refresh_sec,
            stop=stop,
        ),
        name=_PRESENCE_THREAD,
        daemon=True,
    )
    presence_thread.start()
    return DiscordHandle(
        stop=stop,
        thread=thread,
        presence_thread=presence_thread,
        presence_join_sec=refresh_sec
        + presence.TYPING_CALL_TIMEOUT_SEC
        + presence.PRESENCE_JOIN_GRACE_SEC,
        scheduler=scheduler,
        task_names=[_DRAIN_TASK],
    )
