"""Telegram outbound mechanism: the `deliver` / `typing` callables the core outbound policy calls.

Each builds a transport client from the current token and performs one send. The core policy
(when to send, FIFO, bump-and-retry, the typing-pulse gate) lives in `channel.outbound`; these are
only the Telegram-specific act of putting bytes on the wire.
"""

from __future__ import annotations

from ... import secrets
from .transport import ChannelError, TelegramClient


def _client(config) -> TelegramClient:
    token = secrets.read_telegram_bot_token()
    if not token:
        # Only reachable in a crash window (chat bound, token gone); the core gates on chat_id.
        raise ChannelError("telegram token missing")
    return TelegramClient(token, api_base=config.telegram_api_base)


def make_deliver(config):
    def deliver(chat_id, text) -> None:
        _client(config).send_message(chat_id, text)

    return deliver


def make_typing(config):
    def typing(chat_id) -> None:
        _client(config).send_chat_action(chat_id, "typing")

    return typing
