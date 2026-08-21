"""Bind a Discord bot to the daemon, authenticated by a one-time nonce carried in a direct message.

The flow (driven by `sellee connect discord` over the localhost control route): validate the
token's shape, prove it with `GET /users/@me`, write it to its 0600 secret file, then mint a
single-use nonce and arm the channel row. Unlike Telegram, there is no one-tap deep link into a
prefilled chat — Discord bots can only be DMed by a user once bot and user share a server, so the
daemon instead returns a zero-permission OAuth invite URL (the bot only ever uses DMs, so it needs
no guild permission at all): the seller adds the bot to any server, then sends it a DM containing
exactly the nonce. `gateway.py`'s awaiting-bind state watches for that DM the same way the Telegram
poller watches for a `/start` carrying the matching payload — first (and only) match binds.

The nonce is returned to the connecting localhost caller only (never an event, never a log) —
the same trust boundary Telegram's own bind embeds its nonce in `start_url` under — and it expires
after `BIND_NONCE_TTL_SEC`, on the shared arming path, so an abandoned bind lapses to `off` here
too rather than leaving the bot adoptable by whoever ends up with the code.
"""

from __future__ import annotations

import re
import secrets as _stdlib_secrets
import time

from sellee import secrets
from sellee.channel.discord.transport import ChannelError, DiscordClient
from sellee.store import BIND_NONCE_TTL_SEC, bind_nonce_live

# Discord bot token shape: three dot-separated base64url segments (user id, timestamp, HMAC).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,80}$")


class BindError(Exception):
    """A bind failure with a machine-readable `kind` so the control route maps it to a status
    (bad_token_format -> 400, unauthorized -> 401, api_error -> 502). Never carries the token."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def is_valid_token_format(token: str) -> bool:
    return bool(_TOKEN_RE.match((token or "").strip()))


def _default_client(token: str, config) -> DiscordClient:
    return DiscordClient(token, api_base=config.discord_api_base)


def _mint_nonce() -> str:
    return _stdlib_secrets.token_urlsafe(24)


def connect_discord(store, config, token, *, make_client=_default_client, mint_nonce=_mint_nonce):
    """Validate + prove the token, persist it, arm a fresh bind nonce, and return the bot's
    identity, its OAuth invite URL, and the nonce. Raises BindError on a bad shape, a rejected
    token, or an API error. The token is written BEFORE the nonce is armed, so a crash between the
    two fails toward off (token present, no nonce) rather than a live-but-unarmed channel."""
    token = (token or "").strip()
    if not is_valid_token_format(token):
        raise BindError(
            "bad_token_format", "token format looks wrong (expected three dot-separated segments)"
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
        raise BindError("api_error", "GET /users/@me returned no username")
    try:
        app = client.get_application()
    except ChannelError as exc:
        raise BindError("api_error", str(exc)) from None
    application_id = app.get("id") if isinstance(app, dict) else None
    if not application_id:
        raise BindError("api_error", "GET /oauth2/applications/@me returned no id")
    secrets.write_discord_bot_token(token)
    nonce = mint_nonce()
    store.arm_bind(
        bot_username, nonce, adapter="discord", expires_ts=time.time() + BIND_NONCE_TTL_SEC
    )
    invite_url = (
        f"https://discord.com/oauth2/authorize?client_id={application_id}&scope=bot&permissions=0"
    )
    return {
        "bot_username": bot_username,
        "application_id": application_id,
        "invite_url": invite_url,
        "nonce": nonce,
    }


def channel_status(store) -> dict:
    """Discord's own bind state, in the shape Telegram's `channel_status` returns — `adapter`
    included, so a caller reading `bound` knows which provider it is bound about."""
    ch = store.get_channel()
    token = secrets.read_discord_bot_token()
    bound = token is not None and ch["chat_id"] is not None and ch["adapter"] == "discord"
    awaiting = (
        token is not None and not bound and bind_nonce_live(ch) and ch["adapter"] == "discord"
    )
    return {
        "adapter": "discord",
        "bound": bound,
        "awaiting_bind": awaiting,
        "bot_username": ch["bot_username"],
        "chat_id": ch["chat_id"] if bound else None,
    }
