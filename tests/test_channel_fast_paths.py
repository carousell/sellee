"""The deterministic fast paths: exact-token matching, instant acks, the renders, and the
inline-keyboard callback round-trip. Driven through the poller against the fake Bot API so the
whole ingest -> dispatch -> reply path runs, with the channel bound.
"""

from __future__ import annotations

import threading

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from sellee import secrets, settings
from sellee.channel import fastpaths
from sellee.channel.fastpaths import (
    CB_NEEDS_ME,
    CB_PAUSE,
    CB_RESUME,
    CB_SKIP_CTA,
    CB_WATCH,
    WATCH_OFF_LABEL,
    WATCH_ON_LABEL,
)
from sellee.channel.telegram.poller import Poller
from sellee.channel.telegram.transport import TelegramClient
from sellee.config import Config


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
    store.arm_bind("sellee_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1, nonce=store.get_channel()["bind_nonce"])


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


def test_sellee_card_shows_state_and_control_row(store, bus, xdg_tmp) -> None:
    _bound(store)
    # A marketplace is only connectable where the seller actually sells, so the Connections block
    # needs a region to have anything to list.
    store.set_seller_config_section("basics", {"region": "SG"})
    with FakeTelegramAPI() as api:
        api.inject_command("/sellee")
        _poller(store, bus, api).tick()
        msg = api.outbox[-1]
    assert "where things stand" in msg["text"]
    assert "plain language" in msg["text"]  # the free-text invitation, not a numbered menu
    assert "Marketplaces I can work for you" in msg["text"]
    # The control row is the three agent controls, then one connect/disconnect per marketplace —
    # a flat spec the provider wraps, so the row grows with the marketplaces rather than being
    # capped. Only the leading three are pinned here; the connector buttons have their own tests.
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons][:3] == [
        CB_PAUSE,
        CB_NEEDS_ME,
        CB_WATCH,
    ]  # active -> Pause toggle; watch off -> the button offers to watch


def test_sellee_card_invites_the_first_listing_when_nothing_is_listed(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/sellee")
        _poller(store, bus, api).tick()
        text = api.outbox[-1]["text"]
    assert "none yet — send a photo to start your first" in text


def test_sellee_card_counts_live_in_progress_and_sold(store, xdg_tmp) -> None:
    store.create_item(title="Chair", list_price=20.0, currency="SGD")  # draft: in progress
    live = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(live["id"], "carousell", "https://carousell.com/p/1")
    sold = store.create_item(title="Desk", list_price=50.0, currency="SGD")
    store.record_listing_url(sold["id"], "carousell", "https://carousell.com/p/2")
    store.create_thread(
        thread_id="carousell:t9",
        side="sell",
        market="carousell",
        counterpart_handle="buyer9",
        item_id=sold["id"],
    )
    store.negotiate_confirm_sold(sold["id"], "carousell:t9")

    text = fastpaths.render_settings_card(store)
    assert "Listings: 1 live, 1 in progress, 1 sold" in text


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


# --- watch mode -------------------------------------------------------------------------------


def test_watch_command_turns_watching_on_and_says_where_the_window_is(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_command("/watch")
        _poller(store, bus, api).tick()
        text = api.outbox[-1]["text"]
    assert settings.get(store, "watch_browser") is True
    assert "Watch mode on" in text
    assert "Chrome window" in text  # where to look, because this is read on a phone


def test_watch_command_toggles_back_off(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        p = _poller(store, bus, api)
        api.inject_command("/watch")
        p.tick()
        api.inject_command("/watch")
        p.tick()
        assert "background" in api.outbox[-1]["text"]
    assert settings.get(store, "watch_browser") is False


def test_watch_tap_flips_whatever_is_set_when_it_lands(store, bus, xdg_tmp) -> None:
    # The button carries no value, so a tap from old scrollback flips the live state rather than
    # restoring the one that was true when the message was sent.
    _bound(store)
    settings.set_now(store, bus, key="watch_browser", raw_value=True)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_WATCH)
        _poller(store, bus, api).tick()
        assert api.answered == ["cbq1"]  # the spinner was cleared
    assert settings.get(store, "watch_browser") is False


def test_watch_button_confirms_once_and_offers_the_way_back(store, bus, xdg_tmp) -> None:
    # A channel door replies synchronously, so it must not also queue the settings echo notice —
    # that would deliver the same confirmation twice to the same chat. The refreshed row is the
    # undo: its label now offers the opposite.
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_WATCH)
        _poller(store, bus, api).tick()
        buttons = api.outbox[-1]["reply_markup"]["inline_keyboard"][0]
    assert store.list_queued_notices() == []
    # Index rather than [-1]: the connector buttons follow the three agent controls.
    assert buttons[2] == {"text": WATCH_OFF_LABEL, "callback_data": CB_WATCH}


def test_control_row_watch_button_reflects_the_setting(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_NEEDS_ME)
        _poller(store, bus, api).tick()
        buttons = api.outbox[-1]["reply_markup"]["inline_keyboard"][0]
    assert buttons[2] == {"text": WATCH_ON_LABEL, "callback_data": CB_WATCH}  # off -> offers on


def test_watch_state_is_on_the_sellee_card_at_its_default(store, bus, xdg_tmp) -> None:
    assert "Watch mode: off" in fastpaths.render_settings_card(store)


def test_skip_cta_tap_acks_and_records_the_skip(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_SKIP_CTA)
        _poller(store, bus, api).tick()
        assert api.answered == ["cbq1"]  # the spinner was cleared
        assert "whenever you're ready" in api.outbox[-1]["text"]
    assert store.get_meta("first_listing_cta_skipped_ts") is not None  # the nudge lane reads this
    assert store.count_pending_inbox() == 0  # handled, never routed to a pass


def test_skip_cta_tap_is_idempotent(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        p = _poller(store, bus, api)
        api.inject_tap(CB_SKIP_CTA)
        p.tick()
        api.inject_tap(CB_SKIP_CTA)  # a stale tap, months later — buttons live forever in history
        p.tick()
        assert len(api.outbox) == 2  # re-acked, harmless
    assert store.count_pending_inbox() == 0
