"""Telegram outbound mechanism: the `deliver` / `typing` callables the core outbound policy calls.

Each builds a transport client from the current token and performs one send. The core policy
(when to send, FIFO, bump-and-retry, and whether the chat should be lit) lives in
`channel.outbound`, and the indicator's cadence in `channel.presence`; these are only the
Telegram-specific act of putting bytes on the wire.
"""

from __future__ import annotations

from sellee import secrets
from sellee.channel.telegram import commands
from sellee.channel.telegram.transport import ChannelError, TelegramClient

# How long the Bot API holds a chat action for: it documents `sendChatAction` as setting the status
# for 5 seconds, or until the bot sends a message to that chat — whichever comes first. The refresh
# cadence is derived from this (`presence.refresh_interval_sec`) rather than written down beside it,
# so the platform's fact lives next to the platform's mechanism and the derivation lives once.
TYPING_INDICATOR_LIFETIME_SEC = 5.0


def _client(config) -> TelegramClient:
    token = secrets.read_telegram_bot_token()
    if not token:
        # Only reachable in a crash window (chat bound, token gone); the core gates on chat_id.
        raise ChannelError("telegram token missing")
    return TelegramClient(token, api_base=config.telegram_api_base)


def make_deliver(config):
    def deliver(chat_id, text, controls=None) -> None:
        # A notice may carry provider-neutral controls (an approval notice's Approve/Cancel, an echo
        # notice's Undo); render them into an inline keyboard on the last chunk.
        _client(config).send_message(chat_id, text, reply_markup=commands.render_controls(controls))

    return deliver


def make_typing(config, *, timeout: float | None = None):
    """The typing mechanism. `timeout` bounds the one call — the keeper passes its own, far shorter
    than the client's 60s default, because a pulse still in flight when the indicator expires has
    already missed its only chance to be useful."""

    def typing(chat_id) -> None:
        _client(config).send_chat_action(chat_id, "typing", timeout=timeout)

    return typing
