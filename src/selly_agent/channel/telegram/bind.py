"""Bind a Telegram bot to the daemon, authenticated by a one-time deep-link nonce.

The flow (driven by `selly-agent connect telegram` over the localhost control route): validate the
BotFather token's shape, prove it with getMe, write the token to its 0600 secret file, then mint a
single-use nonce and arm the channel row for it. The daemon returns a deep link
`t.me/<bot>?start=<nonce>` — only the chat that presents that nonce in its /start ever binds, so the
hijack race a first-contact capture would have is gone by construction.

The nonce is returned only to the connecting caller (embedded in the deep link) and never enters an
event or a log. The token travels token-file only — never argv, never an event, never error text
(the transport strips any URL echo). A crash between the token write and arming the nonce leaves a
token with no nonce → the poller reads `off`, cured by re-running connect.
"""

from __future__ import annotations

import secrets as _stdlib_secrets

from selly_agent import secrets
from selly_agent.channel.telegram.transport import (
    ChannelError,
    TelegramClient,
    is_valid_token_format,
)


class BindError(Exception):
    """A bind failure with a machine-readable `kind` so the control route maps it to a status
    (bad_token_format -> 400, unauthorized -> 401, api_error -> 502). Never carries the token."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _default_client(token: str, config) -> TelegramClient:
    return TelegramClient(token, api_base=config.telegram_api_base)


def _mint_nonce() -> str:
    # token_urlsafe(24) is ~32 chars — comfortably inside Telegram's 64-char start-payload limit.
    return _stdlib_secrets.token_urlsafe(24)


def connect_telegram(store, config, token, *, make_client=_default_client, mint_nonce=_mint_nonce):
    """Validate + prove the token, persist it, arm a fresh bind nonce, and return the deep link.

    Returns {bot_username, start_url}. Raises BindError on a bad shape, a rejected token, or an API
    error. The token is written BEFORE the nonce is armed, so a crash between the two fails toward
    off (token present, no nonce) rather than a live-but-unarmed channel."""
    token = (token or "").strip()
    if not is_valid_token_format(token):
        raise BindError(
            "bad_token_format", "token format looks wrong (expected <digits>:<35+ chars>)"
        )
    client = make_client(token, config)
    try:
        me = client.get_me()
    except ChannelError as exc:
        message = str(exc)
        kind = "unauthorized" if "HTTP 401" in message else "api_error"
        raise BindError(kind, message) from None
    bot_username = me.get("username") if isinstance(me, dict) else None
    if not bot_username:
        raise BindError("api_error", "getMe returned no bot username")
    secrets.write_telegram_bot_token(token)
    nonce = mint_nonce()
    store.arm_bind(bot_username, nonce)
    return {"bot_username": bot_username, "start_url": f"https://t.me/{bot_username}?start={nonce}"}


def channel_status(store) -> dict:
    """The bind state for the connecting CLI to poll: bound / awaiting_bind / off, plus the bot
    username (and, once bound, the chat id — returned only to the localhost caller)."""
    ch = store.get_channel()
    token = secrets.read_telegram_bot_token()
    bound = token is not None and ch["chat_id"] is not None
    awaiting = token is not None and not bound and ch["bind_nonce"] is not None
    return {
        "bound": bound,
        "awaiting_bind": awaiting,
        "bot_username": ch["bot_username"],
        "chat_id": ch["chat_id"] if bound else None,
    }
