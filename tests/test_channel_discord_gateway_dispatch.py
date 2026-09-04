"""DiscordGateway: the off/awaiting-bind/bound state derivation, nonce-in-a-DM bind matching, and
fast-path dispatch — the Discord analog of tests/test_channel_bind.py + the poller half of
test_channel_transport.py, driven against the fake REST API for sends and a direct on_dispatch
call for the Gateway side. The full-stack WS round trip is covered by
tests/test_channel_discord_gateway_session.py, so these inject dispatch payloads directly rather
than re-proving the transport.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fake_discord_api import CHANNEL_ID, FAKE_TOKEN, FakeDiscordAPI
from sellee import secrets
from sellee.channel import acks, fastpaths
from sellee.channel.discord import gateway
from sellee.channel.discord.gateway import DiscordGateway
from sellee.channel.discord.transport import DiscordClient
from sellee.config import Config

_NONCE = "nonce-abc123"


def _gateway(store, bus, api):
    config = Config(discord_api_base=api.base_url + "/api/v10")
    return DiscordGateway(store=store, config=config, bus=bus)


def _client(api) -> DiscordClient:
    return DiscordClient(FAKE_TOKEN, api_base=api.base_url + "/api/v10")


def test_state_off_with_no_token(store) -> None:
    gw = DiscordGateway(store=store, config=Config(), bus=None)
    assert gw._state(None, store.get_channel()) == "off"


def test_state_awaiting_bind_with_token_and_nonce(store, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    gw = DiscordGateway(store=store, config=Config(), bus=None)
    assert gw._state(FAKE_TOKEN, store.get_channel()) == "awaiting_bind"


def test_state_bound_once_chat_id_is_set(store, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    store.complete_bind(CHANNEL_ID, 0, nonce=store.get_channel()["bind_nonce"])
    gw = DiscordGateway(store=store, config=Config(), bus=None)
    assert gw._state(FAKE_TOKEN, store.get_channel()) == "bound"


def test_a_dm_matching_the_nonce_binds(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_awaiting_bind_message(
            {"id": "1", "channel_id": str(CHANNEL_ID), "author": {"bot": False}, "content": _NONCE}
        )
        ch = store.get_channel()
        assert ch["chat_id"] == CHANNEL_ID
        assert ch["bind_nonce"] is None


def test_a_dm_not_matching_the_nonce_never_binds(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_awaiting_bind_message(
            {"id": "1", "channel_id": str(CHANNEL_ID), "author": {"bot": False}, "content": "wrong"}
        )
        assert store.get_channel()["chat_id"] is None


def test_state_off_once_the_nonce_expires(store, xdg_tmp) -> None:
    """The TTL rides on the shared arming path, so an abandoned Discord bind lapses exactly like a
    Telegram one — and off means no WebSocket is held open for it."""
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord", expires_ts=time.time() - 1)
    gw = DiscordGateway(store=store, config=Config(), bus=None)
    assert gw._state(FAKE_TOKEN, store.get_channel()) == "off"


def test_a_dm_matching_an_expired_nonce_never_binds(store, bus, xdg_tmp) -> None:
    """A gateway session outlives the deadline, so the DM handler re-checks it: the right code,
    arriving too late, is dropped like any other unattributable pre-bind traffic."""
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord", expires_ts=time.time() - 1)
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_awaiting_bind_message(
            {"id": "1", "channel_id": str(CHANNEL_ID), "author": {"bot": False}, "content": _NONCE}
        )
        assert store.get_channel()["chat_id"] is None


def test_an_expired_nonce_is_retired_on_the_next_pass(store, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord", expires_ts=time.time() - 1)
    gw = DiscordGateway(store=store, config=Config(), bus=None)
    gw._retire_lapsed_nonce(store.get_channel())
    ch = store.get_channel()
    assert ch["bind_nonce"] is None
    assert ch["bind_nonce_expires_ts"] is None


def test_fast_path_command_replies_and_marks_handled(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    store.complete_bind(CHANNEL_ID, 0, nonce=store.get_channel()["bind_nonce"])
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_bound_message(
            {
                "id": "1",
                "channel_id": str(CHANNEL_ID),
                "author": {"bot": False},
                "content": "/status",
            }
        )
        assert any("Status:" in m["content"] for m in api.outbox)


def test_free_text_stays_pending_for_the_channel_pass(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    store.complete_bind(CHANNEL_ID, 0, nonce=store.get_channel()["bind_nonce"])
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_bound_message(
            {
                "id": "1",
                "channel_id": str(CHANNEL_ID),
                "author": {"bot": False},
                "content": "how much for the lamp",
            }
        )
    assert store.has_active_channel_pass()


def _bound(store) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    store.complete_bind(CHANNEL_ID, 0, nonce=store.get_channel()["bind_nonce"])


def _dm(content="hello", event_id="1", channel_id=CHANNEL_ID, attachments=None):
    return {
        "id": event_id,
        "channel_id": str(channel_id),
        "author": {"id": "555", "bot": False},
        "content": content,
        "attachments": attachments or [],
        "timestamp": "2026-08-12T00:00:00+00:00",
    }


def test_a_dm_in_another_channel_is_never_ingested(store, bus, xdg_tmp) -> None:
    """The bot shares a server with the seller, so it can be messaged in channels the seller never
    bound. Only the authorized DM is ingested — anything else is dropped before it can reach a
    pass and be answered as if the seller had sent it."""
    _bound(store)
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_bound_message(_dm(channel_id=CHANNEL_ID + 1))
    assert store.count_pending_inbox() == 0
    # Also that no pass was queued: an ingested row is immediately claimed into one, so the
    # pending count alone would read 0 whether the message was dropped or answered.
    assert not store.has_active_channel_pass()


def test_bound_downloads_the_photo_before_ingest(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeDiscordAPI() as api:
        attachment = {
            "id": "a1",
            "filename": "photo.jpg",
            "url": api.base_url + "/attachments/fake.jpg",
            "size": 123,
        }
        _gateway(store, bus, api)._handle_bound_message(_dm("for sale", attachments=[attachment]))

    queued = [e for e in bus.store.read() if e.kind == "pass.queued"]
    rows = store.inbox_for_pass(queued[0].pass_id)
    assert rows[0]["kind"] == "photo"
    assert Path(rows[0]["media_paths"][0]).read_bytes() == b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def test_a_failed_photo_download_still_ingests_the_message(store, bus, xdg_tmp) -> None:
    """A CDN failure must not swallow the message: the seller's caption is still theirs to answer,
    and losing the row would leave them waiting on a reply that never comes."""
    _bound(store)
    with FakeDiscordAPI() as api:
        attachment = {
            "id": "a1",
            "filename": "photo.jpg",
            "url": api.base_url + "/api/v10/nope",  # 401, not an attachment path
            "size": 123,
        }
        _gateway(store, bus, api)._handle_bound_message(_dm("for sale", attachments=[attachment]))

    queued = [e for e in bus.store.read() if e.kind == "pass.queued"]
    rows = store.inbox_for_pass(queued[0].pass_id)
    assert rows[0]["kind"] == "photo"
    assert rows[0]["text"] == "for sale"
    assert rows[0]["media_paths"] == []


# --- interactions (button clicks arrive over the Gateway, are acknowledged over REST) ---------


def _interaction(choice="pause", event_id="111222333444555666", channel_id=CHANNEL_ID, type_=3):
    return {
        "id": event_id,
        "token": "int-token",
        "type": type_,
        "channel_id": str(channel_id),
        "data": {"custom_id": choice},
    }


def test_a_button_click_is_acknowledged_and_answered(store, bus, xdg_tmp) -> None:
    """Discord requires a REST callback for an interaction even though it arrived over the
    Gateway — without it the seller's client shows the click as failed, whatever the bot then
    says. The fast-path reply follows as an ordinary message."""
    _bound(store)
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_interaction(_interaction("pause"), client=_client(api))

    assert api.acknowledged  # the callback fired
    assert any("Paused" in m["content"] for m in api.outbox)
    assert store.is_paused()


def test_an_interaction_in_another_channel_is_dropped(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_interaction(
            _interaction("pause", channel_id=CHANNEL_ID + 1), client=_client(api)
        )
        assert not api.acknowledged
    assert not store.has_active_channel_pass()
    assert not store.is_paused()


def test_a_non_component_interaction_is_ignored(store, bus, xdg_tmp) -> None:
    """Only MESSAGE_COMPONENT (type 3) is ours. Slash commands and autocomplete are not registered
    by this provider, so anything else arriving is not something we can answer."""
    _bound(store)
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_interaction(
            _interaction("pause", type_=2), client=_client(api)
        )
        assert not api.acknowledged
    assert not store.has_active_channel_pass()


# --- bind side effects -------------------------------------------------------------------------


def test_binding_queues_the_welcome_and_publishes_channel_bound(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_awaiting_bind_message(
            _dm(content=_NONCE) | {"author": {"id": "555", "bot": False}}
        )

    ch = store.get_channel()
    assert ch["welcomed_at"]  # stamped, so a reconnect never re-greets
    assert store.list_queued_notices()  # the greeting is queued, not fire-and-forget
    assert [e for e in bus.store.read() if e.kind == "channel.bound"]


def test_a_bot_authored_dm_never_binds(store, bus, xdg_tmp) -> None:
    """The bot's own messages echo back over the Gateway, and other bots can DM it. Neither may
    complete a bind, even holding the nonce."""
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", _NONCE, adapter="discord")
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_awaiting_bind_message(
            {
                "id": "1",
                "channel_id": str(CHANNEL_ID),
                "author": {"id": "999", "bot": True},
                "content": _NONCE,
            }
        )
    assert store.get_channel()["chat_id"] is None


# --- run()'s throttling with no stop_event (the constructor's own default) --------------------
#
# A bare DiscordGateway() — no daemon, no stop_event supplied — must never busy-spin: the
# off-idle wait and the error-backoff wait inside run() have to keep sleeping for real even
# though there's no real threading.Event to wait on. Covered two ways: a direct, deterministic
# unit test of _NeverStop.wait() itself (no need to touch run()'s loop at all), and a timed
# run() call bounded to a handful of iterations (run() has no natural exit with stop=None, so the
# bound is a monkeypatched _NeverStop.wait that raises after N calls — the same technique a real
# stop_event's is_set() flipping True would trigger, just deterministic instead of racy).


def test_neverstop_wait_actually_sleeps() -> None:
    stopper = gateway._NeverStop()
    started = time.monotonic()
    result = stopper.wait(0.05)
    elapsed = time.monotonic() - started
    assert result is False
    assert elapsed >= 0.05


class _StopTestLoop(Exception):
    """Raised from a monkeypatched _NeverStop.wait to give run()'s otherwise-infinite
    (stop_event=None) loop a deterministic exit after a fixed number of iterations."""


def test_run_throttles_the_off_idle_wait_with_no_stop_event(store, xdg_tmp, monkeypatch) -> None:
    """No token is ever written (xdg_tmp keeps secrets hermetic), so _state is always "off" and
    run() only ever takes the off-idle branch. OFF_IDLE_SEC is monkeypatched down so the test
    stays fast; _NeverStop.wait is wrapped to count real calls and bail out after a few — proving
    run() calls a real, sleeping wait() on every iteration (not a no-op), the exact gap that let
    a stop_event=None DiscordGateway busy-spin at 100% CPU before this fix."""
    monkeypatch.setattr(gateway, "OFF_IDLE_SEC", 0.05)
    real_wait = gateway._NeverStop.wait
    calls: list = []

    def counting_wait(self, timeout):
        calls.append(timeout)
        if len(calls) >= 3:
            raise _StopTestLoop
        return real_wait(self, timeout)

    monkeypatch.setattr(gateway._NeverStop, "wait", counting_wait)
    gw = DiscordGateway(store=store, config=Config(), bus=None)  # stop_event defaults to None
    started = time.monotonic()
    with pytest.raises(_StopTestLoop):
        gw.run()
    elapsed = time.monotonic() - started
    assert calls == [0.05, 0.05, 0.05]
    # Two real sleeps must have elapsed before the third call raised — a busy spin would finish
    # in microseconds regardless of OFF_IDLE_SEC.
    assert elapsed >= 0.05 * 2


def test_a_decision_tap_is_acknowledged_but_left_for_the_pass(store, bus, xdg_tmp) -> None:
    """A tappable ask's answer is the channel pass's work (compose the buyer reply, mint the link),
    so it must not be answered here — but Discord still needs the interaction acknowledged, or the
    seller's client reports the click as failed."""
    _bound(store)
    notice_id = store.queue_notice(
        "Needs your call: meet at Orchard, or checkout?",
        options=["🔗 Send checkout link", "🤝 I'll handle it"],
    )
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_interaction(
            _interaction(f"n{notice_id}:a0"), client=_client(api)
        )
        assert api.acknowledged  # acked even though no fast path ran
        assert api.outbox == []  # nothing answered deterministically

    rows = store._db.query("SELECT text, status FROM channel_inbox")
    assert len(rows) == 1
    assert rows[0]["text"] == "🔗 Send checkout link"  # the words, not the token
    assert rows[0]["status"] == "claimed"
    assert store.has_active_channel_pass()


def test_a_decision_tap_is_receipted_on_discord_too(store, bus, xdg_tmp) -> None:
    """Parity, and it is not decorative: Discord splits ingest across two handlers where Telegram
    has one loop, so the tail is exactly the shape that gets added to one and forgotten on the
    other. Both go through routing.settle_batch."""
    _bound(store)
    notice_id = store.queue_notice(
        "Needs your call: meet at Orchard, or checkout?",
        options=["🔗 Send checkout link", "🤝 I'll handle it"],
    )
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_interaction(
            _interaction(f"n{notice_id}:a0"), client=_client(api)
        )
        assert api.typing_pulses == [True]

    receipts = [n["text"] for n in store.list_queued_notices() if n["text"].startswith("Got it:")]
    assert receipts == ["Got it: 🔗 Send checkout link. " + acks.WORKING]


def test_a_typed_message_is_receipted_on_discord_too(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_bound_message(
            _dm("is the lamp still available?"), client=_client(api)
        )

    assert [n["text"] for n in store.list_queued_notices()] == [acks.WORKING]


def test_a_fast_path_click_is_never_also_receipted_on_discord(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_interaction(
            _interaction(fastpaths.CB_PAUSE), client=_client(api)
        )

    assert store.list_queued_notices() == []


def test_answering_an_ask_takes_its_buttons_away_on_discord(store, bus, xdg_tmp) -> None:
    """Type 7 acknowledges the click and strips the components in one request, so the Gateway pump
    thread — the one that has to keep heartbeating — pays for one REST call, not two."""
    _bound(store)
    notice_id = store.queue_notice("meet, or checkout?", options=["Checkout", "Myself"])
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_interaction(
            _interaction(f"n{notice_id}:a0"), client=_client(api)
        )

    assert api.callbacks == [{"type": 7, "data": {"components": []}}]


def test_a_control_row_click_keeps_its_buttons_on_discord(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeDiscordAPI() as api:
        _gateway(store, bus, api)._handle_interaction(
            _interaction(fastpaths.CB_PAUSE), client=_client(api)
        )

    assert api.callbacks == [{"type": 6}]
