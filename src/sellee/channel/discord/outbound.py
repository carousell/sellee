"""Discord outbound mechanism: the `deliver` / `typing` callables the core outbound policy calls.

Each builds a transport client from the current token and performs one send. The core policy (when
to send, FIFO, bump-and-retry, and whether the chat should be lit) lives in `channel.outbound`, and
the indicator's cadence in `channel.presence`; these are only the Discord-specific act of putting
bytes on the wire.
"""

from __future__ import annotations

from sellee import secrets
from sellee.channel.discord.transport import ChannelError, DiscordClient

# How long Discord holds the typing indicator for: it documents the trigger as expiring after 10
# seconds, or the moment the bot posts to the channel. Twice Telegram's, which is why the single
# shared 4.0s pulse interval this replaced was never wrong here — it was Telegram-shaped, and
# Discord's coverage was an undocumented coincidence. Each platform's fact now sits next to its own
# mechanism, and `presence.refresh_interval_sec` derives the cadence from it.
TYPING_INDICATOR_LIFETIME_SEC = 10.0


def _client(config) -> DiscordClient:
    token = secrets.read_discord_bot_token()
    if not token:
        # Only reachable in a crash window (chat bound, token gone); the core gates on chat_id.
        raise ChannelError("discord token missing")
    return DiscordClient(token, api_base=config.discord_api_base)


def make_deliver(config):
    def deliver(chat_id, text, controls=None) -> None:
        _client(config).send_message(chat_id, text, components=controls)

    return deliver


def make_typing(config, *, timeout: float | None = None):
    """The typing mechanism. `timeout` bounds the one call — the keeper passes its own, far shorter
    than the client's 60s default, because a pulse still in flight when the indicator expires has
    already missed its only chance to be useful."""

    def typing(chat_id) -> None:
        _client(config).trigger_typing(chat_id, timeout=timeout)

    return typing
