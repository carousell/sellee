"""Discord's connect flow: validate + prove the token, persist it, arm a fresh bind nonce, and
return the OAuth invite URL — driven against the real DiscordClient over the fake REST API.
"""

from __future__ import annotations

import time

import pytest

from fake_discord_api import APPLICATION_ID, BOT, FAKE_TOKEN, FakeDiscordAPI
from sellee import secrets
from sellee.channel.discord.bind import BindError, channel_status, connect_discord
from sellee.channel.discord.transport import DiscordClient
from sellee.config import Config
from sellee.store import BIND_NONCE_TTL_SEC


def _make_client_for(api):
    def _make(token, config):
        return DiscordClient(token, api_base=api.base_url + "/api/v10")

    return _make


def test_connect_discord_writes_token_arms_nonce_and_returns_invite_url(store, xdg_tmp) -> None:
    with FakeDiscordAPI() as api:
        result = connect_discord(store, Config(), FAKE_TOKEN, make_client=_make_client_for(api))
        assert result["bot_username"] == BOT["username"]
        assert result["application_id"] == APPLICATION_ID
        assert result["invite_url"] == (
            f"https://discord.com/oauth2/authorize?client_id={APPLICATION_ID}"
            "&scope=bot&permissions=0"
        )
        assert secrets.read_discord_bot_token() == FAKE_TOKEN
        ch = store.get_channel()
        assert ch["adapter"] == "discord"
        assert ch["bind_nonce"] == result["nonce"]
        assert ch["chat_id"] is None


def test_connect_discord_rejects_a_bad_token_shape(store, xdg_tmp) -> None:
    with FakeDiscordAPI() as api:
        with pytest.raises(BindError) as exc:
            connect_discord(store, Config(), "not-a-real-token", make_client=_make_client_for(api))
        assert exc.value.kind == "bad_token_format"
        assert secrets.read_discord_bot_token() is None


def test_channel_status_reports_awaiting_bind_after_connect(store, xdg_tmp) -> None:
    with FakeDiscordAPI() as api:
        connect_discord(store, Config(), FAKE_TOKEN, make_client=_make_client_for(api))
    status = channel_status(store)
    assert status["awaiting_bind"] is True
    assert status["bound"] is False
    assert status["bot_username"] == BOT["username"]


def test_connect_discord_arms_the_nonce_with_a_deadline(store, xdg_tmp) -> None:
    with FakeDiscordAPI() as api:
        before = time.time()
        connect_discord(store, Config(), FAKE_TOKEN, make_client=_make_client_for(api))
    expires_ts = store.get_channel()["bind_nonce_expires_ts"]
    assert expires_ts is not None
    assert before + BIND_NONCE_TTL_SEC <= expires_ts <= time.time() + BIND_NONCE_TTL_SEC


def test_channel_status_reports_an_expired_nonce_as_not_awaiting(store, xdg_tmp) -> None:
    """The connecting CLI polls this, so a lapsed nonce must stop reading as awaiting here too."""
    with FakeDiscordAPI() as api:
        connect_discord(store, Config(), FAKE_TOKEN, make_client=_make_client_for(api))
    store.arm_bind(BOT["username"], "n1", adapter="discord", expires_ts=time.time() - 1)
    status = channel_status(store)
    assert status["awaiting_bind"] is False
    assert status["bound"] is False
