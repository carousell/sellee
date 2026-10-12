"""The Discord provider's lifecycle: is_configured, start spins the Gateway thread and registers
the drain/typing lanes, shutdown tears both down. Mirrors tests/test_channel_manager.py's Telegram
provider coverage.
"""

from __future__ import annotations

from fake_discord_api import FAKE_TOKEN
from sellee import secrets
from sellee.channel.discord import provider
from sellee.config import Config
from sellee.scheduler import Scheduler


def test_not_configured_with_no_token(xdg_tmp) -> None:
    assert provider.is_configured() is False


def test_configured_once_a_token_is_written(xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    assert provider.is_configured() is True


def test_start_registers_the_drain_lane_and_the_presence_keeper_then_shutdown_removes_them(
    store, bus, xdg_tmp
) -> None:
    # `start` spins a real DiscordGateway.run() thread. It stays off the network only because the
    # fresh store leaves the channel row unbound, so run() never leaves its off-idle branch —
    # arming a bind here would dial gateway.discord.gg for real, since the gateway's host
    # constants are what tests override to reach a fake (see the session tests).
    secrets.write_discord_bot_token(FAKE_TOKEN)
    scheduler = Scheduler(bus, tick_interval_sec=100.0)
    handle = provider.start(bus=bus, store=store, config=Config(), scheduler=scheduler)
    assert store.get_channel()["chat_id"] is None
    assert "notice_drain" in scheduler._reg.tasks
    # The indicator is a thread, not a lane: the scheduler's tick is a floor no interval can beat,
    # and its pool is shared with a pass lane that blocks for as long as a pass runs.
    assert "typing_pulse" not in scheduler._reg.tasks
    assert handle.presence_thread.is_alive()
    handle.shutdown()
    assert "notice_drain" not in scheduler._reg.tasks
    assert not handle.presence_thread.is_alive()  # joined, not left pulsing
