"""The deterministic fast paths: exact-token matching, instant acks, the renders, and the
inline-keyboard callback round-trip. Driven through the poller against the fake Bot API so the
whole ingest -> dispatch -> reply path runs, with the channel bound.
"""

from __future__ import annotations

import threading

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from selly_agent import secrets
from selly_agent.channel import fastpaths
from selly_agent.channel.fastpaths import CB_NEEDS_ME, CB_PAUSE, CB_RESUME
from selly_agent.channel.telegram.poller import Poller
from selly_agent.channel.telegram.transport import TelegramClient
from selly_agent.config import Config


def _poller(store, bus, api):
    return Poller(
        store=store,
        config=Config(telegram_api_base=api.base_url),
        bus=bus,
        stop_event=threading.Event(),
        client_factory=lambda token: TelegramClient(FAKE_TOKEN, api_base=api.base_url),
        poll_timeout=0,
    )


def _bound(store):
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1)


def _seed_needs_me(store):
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:t1",
        side="sell",
        market="carousell",
        counterpart_handle="buyer1",
        item_id=item["id"],
    )
    store.escalate("carousell:t1", open_question="Accept $70?")
    store.queue_notice("Your listing went live.")


# --- exact-token matching -------------------------------------------------------------------


def test_pause_command_is_a_fast_path() -> None:
    assert fastpaths.is_fast_path({"kind": "command", "text": "/pause", "payload": {}})


def test_non_exact_token_is_not_a_fast_path() -> None:
    # "please /pause" arrives as kind=text (does not start with '/'), so it is never a fast path.
    assert not fastpaths.is_fast_path({"kind": "text", "text": "please /pause", "payload": {}})


def test_unknown_command_is_not_a_fast_path() -> None:
    assert not fastpaths.is_fast_path({"kind": "command", "text": "/sell", "payload": {}})


# --- instant acks + flag flips --------------------------------------------------------------


def test_pause_acks_instantly_and_sets_flag(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/pause")
        _poller(store, bus, api).tick()
        assert store.is_paused() is True
        assert api.outbox and "Paused" in api.outbox[-1]["text"]
    assert store.count_pending_inbox() == 0  # the command row is handled, never routed


def test_resume_clears_flag(store, bus, xdg_tmp) -> None:
    _bound(store)
    store.set_paused(True, source="telegram")
    with FakeTelegramAPI() as api:
        api.inject_command("/resume")
        _poller(store, bus, api).tick()
        assert store.is_paused() is False


def test_freetext_routes_to_the_pass_not_a_fast_path(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_text("is the lamp still available?")
        _poller(store, bus, api).tick()
        assert api.outbox == []  # not a fast path — no deterministic reply
    assert store.has_active_channel_pass() is True  # it routed to the channel pass instead


# --- renders --------------------------------------------------------------------------------


def test_status_render_counts_waiting(store, bus, xdg_tmp) -> None:
    _bound(store)
    _seed_needs_me(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/status")
        _poller(store, bus, api).tick()
        text = api.outbox[-1]["text"]
    assert "running" in text and "1 decision" in text and "1 update" in text


def test_catchup_render_lists_questions_and_updates(store, bus, xdg_tmp) -> None:
    _bound(store)
    _seed_needs_me(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/catchup")
        _poller(store, bus, api).tick()
        text = api.outbox[-1]["text"]
    assert "Accept $70?" in text and "Your listing went live." in text


def test_selly_card_shows_state_and_control_row(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/selly")
        _poller(store, bus, api).tick()
        msg = api.outbox[-1]
    assert "where things stand" in msg["text"]
    assert "plain language" in msg["text"]  # the free-text invitation, not a numbered menu
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [
        CB_PAUSE,
        CB_NEEDS_ME,
    ]  # active -> Pause toggle


# --- keyboard callback round-trip -----------------------------------------------------------


def test_pause_button_tap_acks_and_pauses(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_PAUSE)
        _poller(store, bus, api).tick()
        assert api.answered == ["cbq1"]  # the spinner was cleared
        assert store.is_paused() is True
        assert api.outbox and "Paused" in api.outbox[-1]["text"]


def test_control_row_toggle_reflects_paused_state(store, bus, xdg_tmp) -> None:
    _bound(store)
    store.set_paused(True, source="telegram")
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_NEEDS_ME)
        _poller(store, bus, api).tick()
        buttons = api.outbox[-1]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == CB_RESUME  # paused -> the toggle offers Resume
