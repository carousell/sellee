"""Bind and the poller state machine — the nonce matrix (never adopt a stranger) plus the two
control routes and the connect flow.

The poller is driven a tick at a time against the in-process fake Bot API (poll_timeout=0 so a
tick never waits), with a real store and the token in its 0600 secret file, so state detection and
the bind transaction are exercised exactly as in production.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from fake_telegram_api import BOT, CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from selly_agent import secrets
from selly_agent.channel.poller import Poller
from selly_agent.channel.telegram import TelegramClient
from selly_agent.config import Config
from selly_agent.http_server import HttpServer

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
        assert any(m["text"] for m in api.outbox)  # welcome sent
    bound = [e for e in bus.store.read() if e.kind == "channel.bound"]
    assert bound and bound[0].payload["bot_username"] == BOT["username"]


def test_welcome_sent_once_across_rebind(store, bus, xdg_tmp) -> None:
    _arm(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/start " + _NONCE)
        _poller(store, bus, api).tick()
        assert len(api.outbox) == 1
    # re-connect the SAME bot, bind again — the welcome stamp survives, so no second greeting
    store.arm_bind(BOT["username"], "nonce-2")
    with FakeTelegramAPI() as api:
        api.inject_command("/start nonce-2")
        _poller(store, bus, api).tick()
        assert api.outbox == []  # never re-greet the same bot


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
    store.complete_bind(CHAT_ID, update_offset=1)


def test_bound_ingests_authorized_chat(store, bus, xdg_tmp) -> None:
    _bind(store)
    with FakeTelegramAPI() as api:
        api.inject_text("is it available?")
        _poller(store, bus, api).tick()
    assert store.count_pending_inbox() == 1
    ins = [e for e in bus.store.read() if e.kind == "channel.in"]
    assert ins and ins[0].payload["preview"] == "is it available?"


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
    rows = store.claim_pending_inbox("x")
    assert rows[0]["kind"] == "photo" and rows[0]["media_paths"]
    from pathlib import Path

    assert Path(rows[0]["media_paths"][0]).read_bytes() == b"\xff\xd8jpeg-bytes"


# --- control routes + connect ----------------------------------------------------------------


class _Server:
    def __init__(self, store, bus, api):
        self.srv = HttpServer(
            port=0,
            bus=bus,
            store=store,
            events_db_path=bus.store.db.path,
            context_factory=lambda s: None,
            attended_token="attended-secret",
            config=Config(telegram_api_base=api.base_url),
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
            "bound": False,
            "awaiting_bind": False,
            "bot_username": None,
            "chat_id": None,
        }
        _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
        awaiting = _get(server, base)
        assert awaiting["awaiting_bind"] is True and awaiting["bound"] is False
        store.complete_bind(CHAT_ID, update_offset=1)
        bound = _get(server, base)
        assert bound["bound"] is True and bound["chat_id"] == CHAT_ID


def test_reconnect_while_bound_re_arms(store, bus, xdg_tmp) -> None:
    _bind(store)
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        status, body = _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
        assert status == 200
        ch = store.get_channel()
        assert ch["chat_id"] is None  # re-arm resets the chat; a fresh /start must re-bind
        assert ch["bind_nonce"] == body["start_url"].split("start=", 1)[1]


def test_bind_attempt_event_carries_only_bot_username(store, bus, xdg_tmp) -> None:
    with FakeTelegramAPI() as api, _Server(store, bus, api) as server:
        _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
    attempts = [e for e in bus.store.read() if e.kind == "channel.bind_attempt"]
    assert attempts and attempts[0].payload == {"bot_username": BOT["username"]}
    # the token appears in no event payload
    for e in bus.store.read():
        assert FAKE_TOKEN not in json.dumps(e.payload)
