"""Bind and the poller state machine — the nonce matrix (never adopt a stranger) plus the two
control routes and the connect flow.

The poller is driven a tick at a time against the in-process fake Bot API (poll_timeout=0 so a
tick never waits), with a real store and the token in its 0600 secret file, so state detection and
the bind transaction are exercised exactly as in production.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from tests.conftest import patch_store_attr

from fake_discord_api import BOT as DISCORD_BOT
from fake_discord_api import FAKE_TOKEN as DISCORD_FAKE_TOKEN
from fake_discord_api import FakeDiscordAPI
from fake_telegram_api import BOT, CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from sellee import secrets
from sellee import store as store_mod
from sellee.channel import fastpaths, outbound
from sellee.channel.telegram.poller import Poller
from sellee.channel.telegram.transport import TelegramClient
from sellee.config import Config
from sellee.http_server import HttpServer

_NONCE = "nonce-abc123"


def _poller(store, bus, api):
    return Poller(
        store=store,
        config=Config(telegram_api_base=api.base_url),
        bus=bus,
        stop_event=threading.Event(),
        client_factory=lambda token: TelegramClient(FAKE_TOKEN, api_base=api.base_url),
        poll_timeout=0,
    )


# --- state: off ------------------------------------------------------------------------------


def test_off_with_no_token_makes_no_api_calls(store, bus, xdg_tmp) -> None:
    with FakeTelegramAPI() as api:
        api.inject_text("hello")  # a message is waiting, but we are off
        _poller(store, bus, api).tick()
        assert api.calls == []  # zero Bot API traffic while off
        assert store.count_pending_inbox() == 0


def test_off_with_token_but_no_nonce_and_no_chat(store, bus, xdg_tmp) -> None:
    secrets.write_telegram_bot_token(FAKE_TOKEN)  # a crash between token write and arm_bind
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
        assert api.calls == []  # still off: token alone is not awaiting-bind


# --- state: awaiting-bind (the nonce matrix) -------------------------------------------------


def _arm(store):
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind(BOT["username"], _NONCE)


def test_stranger_message_never_binds_and_is_discarded(store, bus, xdg_tmp) -> None:
    _arm(store)
    with FakeTelegramAPI() as api:
        api.inject_text("hi bot", chat_id=CHAT_ID + 99)
        _poller(store, bus, api).tick()
    assert store.get_channel()["chat_id"] is None  # never adopted
    assert store.count_pending_inbox() == 0  # nothing ingested pre-bind


def test_bare_or_wrong_start_never_binds(store, bus, xdg_tmp) -> None:
    _arm(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/start")  # no payload
        api.inject_command("/start wrong-nonce")  # wrong payload
        p = _poller(store, bus, api)
        p.tick()
        p.tick()
    assert store.get_channel()["chat_id"] is None


def test_start_with_nonce_binds_atomically(store, bus, xdg_tmp) -> None:
    _arm(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
        ch = store.get_channel()
        assert ch["chat_id"] == CHAT_ID
        assert ch["bind_nonce"] is None  # cleared in the same transaction
        assert ch["update_offset"] > 0  # cursor advanced past the /start
        assert api.commands is not None  # setMyCommands registered
    # the greeting is queued durably (the drain lane delivers it), never fired from the bind tick —
    # and a seller with nothing listed yet gets the first-listing CTA with its Skip button
    queued = store.list_queued_notices()
    assert [n["text"] for n in queued] == [outbound.WELCOME_TEXT, outbound.FIRST_LISTING_CTA_TEXT]
    assert queued[1]["controls"] == [[outbound.CTA_SKIP_LABEL, fastpaths.CB_SKIP_CTA]]
    assert store.get_channel()["welcomed_at"] is not None
    bound = [e for e in bus.store.read() if e.kind == "channel.bound"]
    assert bound and bound[0].payload["bot_username"] == BOT["username"]


def test_welcome_queued_once_across_rebind(store, bus, xdg_tmp) -> None:
    _arm(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
    assert store.count_queued_notices() == 2  # welcome + first-listing CTA
    # re-connect the SAME bot, bind again — the welcome stamp survives, so no second greeting
    store.arm_bind(BOT["username"], "nonce-2")
    with FakeTelegramAPI() as api:
        api.inject_command("/start nonce-2")
        _poller(store, bus, api).tick()
        assert api.outbox == []  # never re-greet the same bot
    assert store.count_queued_notices() == 2


def test_bind_with_an_item_already_listed_skips_the_first_listing_cta(store, bus, xdg_tmp) -> None:
    store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    _arm(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
    # a seller with real usage is greeted but never told to make their "first" listing
    assert [n["text"] for n in store.list_queued_notices()] == [outbound.WELCOME_TEXT]


def test_welcome_queue_failure_leaves_the_bind_complete(store, bus, xdg_tmp, monkeypatch) -> None:
    _arm(store)

    def _boom(store):
        raise RuntimeError("disk full")

    monkeypatch.setattr(outbound, "queue_welcome", _boom)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
    ch = store.get_channel()
    assert ch["chat_id"] == CHAT_ID  # the bind itself survived
    assert ch["welcomed_at"] is None  # unstamped, so a rebind can still greet
    assert [e for e in bus.store.read() if e.kind == "channel.bound"]


def test_expired_nonce_never_binds(store, bus, xdg_tmp) -> None:
    """The armed nonce leaves the machine by design (the CLI has the seller relay the link to a
    phone), so the window it is worth anything in has to close on its own."""
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind(BOT["username"], _NONCE, expires_ts=time.time() - 1)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
    ch = store.get_channel()
    assert ch["chat_id"] is None  # the right nonce, too late
    assert ch["bind_nonce"] is None  # and retired, so nothing keeps a dead secret armed


def test_expired_nonce_drops_the_channel_to_off(store, bus, xdg_tmp) -> None:
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind(BOT["username"], _NONCE, expires_ts=time.time() - 1)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        poller = _poller(store, bus, api)
        assert poller._state(FAKE_TOKEN, store.get_channel()) == "off"
        poller.tick()
        assert api.calls == []  # off, so not even a getUpdates — an abandoned bind stops polling


def test_nonce_within_ttl_still_binds(store, bus, xdg_tmp, monkeypatch) -> None:
    frozen = 1_700_000_000.0
    patch_store_attr(monkeypatch, "_now", lambda: frozen)
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind(BOT["username"], _NONCE)  # the standard TTL, off the frozen clock
    patch_store_attr(monkeypatch, "_now", lambda: frozen + store_mod.BIND_NONCE_TTL_SEC - 1)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
    assert store.get_channel()["chat_id"] == CHAT_ID


def test_advance_offset_does_not_extend_the_nonce_ttl(store, bus, xdg_tmp, monkeypatch) -> None:
    """The deadline is its own column rather than a read of updated_ts, which the awaiting-bind
    path bumps on every batch of unattributable traffic: otherwise a stranger spamming the bot
    would keep the nonce alive for as long as they cared to keep typing."""
    frozen = 1_700_000_000.0
    patch_store_attr(monkeypatch, "_now", lambda: frozen)
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind(BOT["username"], _NONCE)
    expires_ts = store.get_channel()["bind_nonce_expires_ts"]

    late = frozen + store_mod.BIND_NONCE_TTL_SEC - 1
    patch_store_attr(monkeypatch, "_now", lambda: late)
    with FakeTelegramAPI() as api:
        api.inject_text("hi bot", chat_id=CHAT_ID + 99)  # pre-bind spam: acked and discarded
        _poller(store, bus, api).tick()
    ch = store.get_channel()
    assert ch["updated_ts"] == late  # the spam did move updated_ts...
    assert ch["bind_nonce_expires_ts"] == expires_ts  # ...and bought the nonce nothing

    patch_store_attr(monkeypatch, "_now", lambda: frozen + store_mod.BIND_NONCE_TTL_SEC + 1)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
        assert api.calls == []
    assert store.get_channel()["chat_id"] is None


def test_a_nonce_that_lapses_mid_poll_never_binds(store, bus, xdg_tmp, monkeypatch) -> None:
    """A long poll can straddle the deadline, so the tick re-reads it before binding: the /start is
    acked and discarded rather than adopting a chat on a nonce that died while the poll was open."""
    frozen = 1_700_000_000.0
    patch_store_attr(monkeypatch, "_now", lambda: frozen)
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind(BOT["username"], _NONCE)

    class _LapsingClient(TelegramClient):
        def get_updates(self, offset, timeout, allowed_updates) -> list:
            updates = super().get_updates(offset, timeout, allowed_updates)
            patch_store_attr(monkeypatch, "_now", lambda: frozen + store_mod.BIND_NONCE_TTL_SEC + 1)
            return updates

    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        Poller(
            store=store,
            config=Config(telegram_api_base=api.base_url),
            bus=bus,
            stop_event=threading.Event(),
            client_factory=lambda token: _LapsingClient(FAKE_TOKEN, api_base=api.base_url),
            poll_timeout=0,
        ).tick()
    ch = store.get_channel()
    assert ch["chat_id"] is None
    assert ch["update_offset"] > 0  # the /start was still acked, not left to be re-offered


def test_restart_mid_bind_resumes_from_durable_nonce(store, bus, xdg_tmp) -> None:
    _arm(store)
    # a "restart": a fresh poller instance reads the same durable state and still binds
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        Poller(
            store=store,
            config=Config(telegram_api_base=api.base_url),
            bus=bus,
            stop_event=threading.Event(),
            client_factory=lambda token: TelegramClient(FAKE_TOKEN, api_base=api.base_url),
            poll_timeout=0,
        ).tick()
    assert store.get_channel()["chat_id"] == CHAT_ID


# --- state: bound ----------------------------------------------------------------------------


def _bind(store):
    _arm(store)
    store.complete_bind(CHAT_ID, update_offset=1, nonce=store.get_channel()["bind_nonce"])


def test_bound_ingests_authorized_chat(store, bus, xdg_tmp) -> None:
    _bind(store)
    with FakeTelegramAPI() as api:
        api.inject_text("is it available?")
        _poller(store, bus, api).tick()
    ins = [e for e in bus.store.read() if e.kind == "channel.in"]
    assert ins and ins[0].payload["preview"] == "is it available?"
    # a free-text message ingests and then routes to a channel pass
    assert store.has_active_channel_pass() is True


def test_bound_drops_other_chats_before_ingest(store, bus, xdg_tmp) -> None:
    _bind(store)
    with FakeTelegramAPI() as api:
        api.inject_text("from a stranger", chat_id=CHAT_ID + 1)
        _poller(store, bus, api).tick()
    assert store.count_pending_inbox() == 0  # dropped before ingest
    assert store.get_channel()["update_offset"] > 1  # but the cursor still advanced (acked)


def test_bound_downloads_photo_before_ingest(store, bus, xdg_tmp) -> None:
    _bind(store)
    with FakeTelegramAPI() as api:
        api.files["p1"] = b"\xff\xd8jpeg-bytes"
        api.inject_photo("for sale", file_id="p1")
        _poller(store, bus, api).tick()
    # the photo row routed into a channel pass; its media was downloaded before ingest
    queued = [e for e in bus.store.read() if e.kind == "pass.queued"]
    rows = store.inbox_for_pass(queued[0].pass_id)
    assert rows[0]["kind"] == "photo" and rows[0]["media_paths"]
    from pathlib import Path

    assert Path(rows[0]["media_paths"][0]).read_bytes() == b"\xff\xd8jpeg-bytes"


# --- control routes + connect ----------------------------------------------------------------


class _Server:
    def __init__(self, store, bus, api, channels=None, config=None):
        self.srv = HttpServer(
            port=0,
            bus=bus,
            store=store,
            events_db_path=bus.store.db.path,
            context_factory=lambda s: None,
            attended_token="attended-secret",
            config=config or Config(telegram_api_base=api.base_url),
            channels=channels,
        )

    def __enter__(self):
        self.srv.start()
        return self.srv

    def __exit__(self, *exc):
        self.srv.stop()


def _post(server, path, body, token="attended-secret"):
    url = f"http://127.0.0.1:{server.port}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _get(server, path):
    url = f"http://127.0.0.1:{server.port}{path}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def test_connect_route_validates_and_returns_deep_link(store, bus, xdg_tmp) -> None:
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        status, body = _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
        assert status == 200
        assert body["bot_username"] == BOT["username"]
        assert body["start_url"].startswith(f"https://t.me/{BOT['username']}?start=")
        # the token was persisted and the row armed with the nonce from the deep link
        assert secrets.read_telegram_bot_token() == FAKE_TOKEN
        nonce = body["start_url"].split("start=", 1)[1]
        assert store.get_channel()["bind_nonce"] == nonce


def test_connect_route_rejects_bad_token_format(store, bus, xdg_tmp) -> None:
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        status, body = _post(server, "/control/connect-telegram", {"token": "not-a-token"})
        assert status == 400 and body["error"] == "bad_token_format"


def test_connect_route_requires_attended_token(store, bus, xdg_tmp) -> None:
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        status, _ = _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN}, token="nope")
        assert status == 401


def test_channel_status_reports_states(store, bus, xdg_tmp) -> None:
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        base = "/control/channel-status?token=attended-secret"
        assert _get(server, base) == {
            "adapter": "telegram",
            "bound": False,
            "awaiting_bind": False,
            "bot_username": None,
            "chat_id": None,
        }
        _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
        awaiting = _get(server, base)
        assert awaiting["awaiting_bind"] is True and awaiting["bound"] is False
        store.complete_bind(CHAT_ID, update_offset=1, nonce=store.get_channel()["bind_nonce"])
        bound = _get(server, base)
        assert bound["bound"] is True and bound["chat_id"] == CHAT_ID


def test_channel_status_reports_an_expired_nonce_as_not_awaiting(store, bus, xdg_tmp) -> None:
    """The connecting CLI polls this route, so an armed-but-lapsed row must not read as awaiting —
    otherwise the wait keeps promising a link that can no longer bind."""
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        base = "/control/channel-status?token=attended-secret"
        _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
        assert _get(server, base)["awaiting_bind"] is True
        store.arm_bind(BOT["username"], _NONCE, expires_ts=time.time() - 1)
        status = _get(server, base)
        assert status["awaiting_bind"] is False
        assert status["bound"] is False


def test_reconnect_while_bound_re_arms(store, bus, xdg_tmp) -> None:
    _bind(store)
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        status, body = _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
        assert status == 200
        ch = store.get_channel()
        assert ch["chat_id"] is None  # re-arm resets the chat; a fresh /start must re-bind
        assert ch["bind_nonce"] == body["start_url"].split("start=", 1)[1]


class _RecordingChannels:
    def __init__(self):
        self.registered = []

    def register(self, name):
        self.registered.append(name)


def test_connect_route_registers_the_provider(store, bus, xdg_tmp) -> None:
    channels = _RecordingChannels()
    with FakeTelegramAPI() as api, _Server(store, bus, api, channels=channels) as server:
        status, _ = _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
        assert status == 200
    assert channels.registered == ["telegram"]  # a fresh connect brings the provider up at runtime


def test_bind_attempt_event_carries_only_bot_username(store, bus, xdg_tmp) -> None:
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
    attempts = [e for e in bus.store.read() if e.kind == "channel.bind_attempt"]
    assert attempts and attempts[0].payload == {"bot_username": BOT["username"]}
    # the token appears in no event payload
    for e in bus.store.read():
        assert FAKE_TOKEN not in json.dumps(e.payload)


# --- Discord: the same control routes, mirroring the Telegram coverage above -----------------


def _discord_config(api) -> Config:
    return Config(discord_api_base=api.base_url + "/api/v10")


def test_discord_connect_route_validates_and_returns_invite_url(store, bus, xdg_tmp) -> None:
    with FakeDiscordAPI() as api, _Server(store, bus, api, config=_discord_config(api)) as server:
        status, body = _post(server, "/control/connect-discord", {"token": DISCORD_FAKE_TOKEN})
        assert status == 200
        assert body["bot_username"] == DISCORD_BOT["username"]
        assert body["invite_url"].startswith("https://discord.com/oauth2/authorize?client_id=")
        assert secrets.read_discord_bot_token() == DISCORD_FAKE_TOKEN
        assert store.get_channel()["bind_nonce"] == body["nonce"]
        assert store.get_channel()["adapter"] == "discord"


def test_discord_connect_route_rejects_bad_token_format(store, bus, xdg_tmp) -> None:
    with FakeDiscordAPI() as api, _Server(store, bus, api, config=_discord_config(api)) as server:
        status, body = _post(server, "/control/connect-discord", {"token": "not-a-token"})
        assert status == 400 and body["error"] == "bad_token_format"


def test_discord_connect_route_requires_attended_token(store, bus, xdg_tmp) -> None:
    with FakeDiscordAPI() as api, _Server(store, bus, api, config=_discord_config(api)) as server:
        status, _ = _post(
            server, "/control/connect-discord", {"token": DISCORD_FAKE_TOKEN}, token="nope"
        )
        assert status == 401


def test_discord_channel_status_reports_states(store, bus, xdg_tmp) -> None:
    with FakeDiscordAPI() as api, _Server(store, bus, api, config=_discord_config(api)) as server:
        base = "/control/channel-status?token=attended-secret"
        # Nothing connected yet, so the row still names the default adapter and Telegram answers.
        assert _get(server, base) == {
            "adapter": "telegram",
            "bound": False,
            "awaiting_bind": False,
            "bot_username": None,
            "chat_id": None,
        }
        _post(server, "/control/connect-discord", {"token": DISCORD_FAKE_TOKEN})
        awaiting = _get(server, base)
        assert awaiting["awaiting_bind"] is True and awaiting["bound"] is False
        assert awaiting["adapter"] == "discord"
        store.complete_bind(111, update_offset=1, nonce=store.get_channel()["bind_nonce"])
        bound = _get(server, base)
        assert bound["bound"] is True and bound["chat_id"] == 111


def test_discord_connect_route_registers_the_provider(store, bus, xdg_tmp) -> None:
    channels = _RecordingChannels()
    with FakeDiscordAPI() as api:
        cfg = _discord_config(api)
        with _Server(store, bus, api, channels=channels, config=cfg) as server:
            status, _ = _post(server, "/control/connect-discord", {"token": DISCORD_FAKE_TOKEN})
            assert status == 200
    assert channels.registered == ["discord"]


def test_discord_bind_attempt_event_carries_only_bot_username(store, bus, xdg_tmp) -> None:
    with FakeDiscordAPI() as api, _Server(store, bus, api, config=_discord_config(api)) as server:
        _post(server, "/control/connect-discord", {"token": DISCORD_FAKE_TOKEN})
    attempts = [e for e in bus.store.read() if e.kind == "channel.bind_attempt"]
    assert attempts and attempts[0].payload == {"bot_username": DISCORD_BOT["username"]}
    for e in bus.store.read():
        assert DISCORD_FAKE_TOKEN not in json.dumps(e.payload)


def test_channel_status_follows_whichever_adapter_is_actually_bound(store, bus, xdg_tmp) -> None:
    """The channel row is a shared singleton (one adapter bound at a time — see store.arm_bind):
    connecting Discord after Telegram replaces the bound adapter, and /control/channel-status must
    switch to answering from Discord's own channel_status, not keep reporting Telegram's stale
    (now-superseded) bot identity. `adapter` names whose answer it is, so a caller reading `bound`
    cannot mistake one provider's binding for the other's."""
    with FakeTelegramAPI() as tg_api, FakeDiscordAPI() as dc_api:
        cfg = Config(
            telegram_api_base=tg_api.base_url, discord_api_base=dc_api.base_url + "/api/v10"
        )
        with _Server(store, bus, tg_api, config=cfg) as server:
            _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
            base = "/control/channel-status?token=attended-secret"
            status = _get(server, base)
            assert status["bot_username"] == BOT["username"]
            assert status["awaiting_bind"] is True
            assert status["adapter"] == "telegram"

            store.complete_bind(CHAT_ID, update_offset=1, nonce=store.get_channel()["bind_nonce"])
            assert _get(server, base) == {
                "adapter": "telegram",
                "bound": True,
                "awaiting_bind": False,
                "bot_username": BOT["username"],
                "chat_id": CHAT_ID,
            }

            _post(server, "/control/connect-discord", {"token": DISCORD_FAKE_TOKEN})
            status = _get(server, base)
            assert status["bot_username"] == DISCORD_BOT["username"]
            assert status["awaiting_bind"] is True
            assert status["adapter"] == "discord"
            # The Telegram token file survives the switch, so nothing but the adapter keeps this
            # from reading as a live Telegram binding.
            assert status["bound"] is False
            assert store.get_channel()["adapter"] == "discord"
