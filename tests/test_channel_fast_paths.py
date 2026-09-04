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
    CB_WATCH_OFF,
    CB_WATCH_ON,
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


def _buttons(msg) -> list:
    """Every button on a sent message, flattened. Which row a button lands on is a legibility
    decision owned by channel/controls.py, so these tests read the keyboard flat and assert on the
    token — an index into row 0 pinned the packing as a side effect of testing something else."""
    return [b for row in msg["reply_markup"]["inline_keyboard"] for b in row]


def _tokens(msg) -> list:
    return [b["callback_data"] for b in _buttons(msg)]


def _label_for(msg, token) -> str | None:
    return next((b["text"] for b in _buttons(msg) if b["callback_data"] == token), None)


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
    # The spec is the three agent controls then one per marketplace; only the leading three are
    # pinned — the connector buttons have their own tests. Read flat rather than per row: how the
    # buttons pack onto rows is a legibility decision (channel/controls.py) and not this test's.
    assert _tokens(msg)[:3] == [
        CB_PAUSE,
        CB_NEEDS_ME,
        CB_WATCH_ON,
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


def test_watch_tap_applies_the_value_its_label_promised(store, bus, xdg_tmp) -> None:
    # The button carries its own intent, so it does what it says whenever it lands.
    _bound(store)
    settings.set_now(store, bus, key="watch_browser", raw_value=True)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_WATCH_OFF)
        _poller(store, bus, api).tick()
        assert api.answered == ["cbq1"]  # the spinner was cleared
    assert settings.get(store, "watch_browser") is False


def test_a_stale_watch_tap_re_acks_instead_of_flipping_the_state_back(store, bus, xdg_tmp) -> None:
    """The reported bug, from the field. These buttons live in the scrollback forever, so a seller
    tapping "🌙 Work in background" on a card from an hour ago must get what it says — not the
    reverse because the state moved underneath it.

    It used to be a valueless toggle: two taps 71 minutes apart, on two different messages, turned
    watch mode ON and then back off, and the seller reported the button as not working."""
    _bound(store)
    settings.set_now(store, bus, key="watch_browser", raw_value=False)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_WATCH_OFF)  # a stale card offering what is already true
        _poller(store, bus, api).tick()
        text = api.outbox[-1]["text"]

    assert settings.get(store, "watch_browser") is False  # never flipped on
    assert "already" in text.lower()


def test_a_stale_watch_on_tap_is_idempotent_too(store, bus, xdg_tmp) -> None:
    _bound(store)
    settings.set_now(store, bus, key="watch_browser", raw_value=True)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_WATCH_ON)
        _poller(store, bus, api).tick()
        text = api.outbox[-1]["text"]

    assert settings.get(store, "watch_browser") is True
    assert "already" in text.lower()


def test_the_watch_button_offers_the_state_the_seller_is_not_in(store, bus, xdg_tmp) -> None:
    """Each label names what tapping does, and now the token agrees with it — so the pair can be
    read off one another rather than one being a promise the other may break."""
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_NEEDS_ME)
        _poller(store, bus, api).tick()
        off_state = api.outbox[-1]
        api.inject_tap(CB_WATCH_ON)
        _poller(store, bus, api).tick()
        on_state = api.outbox[-1]

    assert _label_for(off_state, CB_WATCH_ON) == WATCH_ON_LABEL
    assert _label_for(off_state, CB_WATCH_OFF) is None  # only ever one of the two
    assert _label_for(on_state, CB_WATCH_OFF) == WATCH_OFF_LABEL


def test_watch_button_confirms_once_and_offers_the_way_back(store, bus, xdg_tmp) -> None:
    # A channel door replies synchronously, so it must not also queue the settings echo notice —
    # that would deliver the same confirmation twice to the same chat. The refreshed row is the
    # undo: its label now offers the opposite.
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_tap(CB_WATCH_ON)
        _poller(store, bus, api).tick()
        msg = api.outbox[-1]
    assert store.list_queued_notices() == []
    assert _label_for(msg, CB_WATCH_OFF) == WATCH_OFF_LABEL


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


# --- buttons from an older release --------------------------------------------------------------


def test_a_retired_button_changes_nothing_and_hands_back_a_working_one(store, bus, xdg_tmp) -> None:
    """Retiring a token does not retire the buttons already sent — they sit in the scrollback
    forever, and the seller cannot tell an old one by looking. `watch` was the valueless toggle
    replaced by CB_WATCH_ON/CB_WATCH_OFF; every card carrying it is still in the chat.

    Its intent is genuinely unrecoverable — the token never said which way it meant, which is the
    bug it was retired for — so the only honest answer is to change nothing and offer live buttons.
    """
    _bound(store)
    settings.set_now(store, bus, key="watch_browser", raw_value=False)
    with FakeTelegramAPI() as api:
        api.inject_tap("watch")
        _poller(store, bus, api).tick()
        msg = api.outbox[-1]

    assert settings.get(store, "watch_browser") is False  # nothing flipped
    assert "older version" in msg["text"]
    assert _label_for(msg, CB_WATCH_ON) == WATCH_ON_LABEL  # a live button, right there
    assert store.has_active_channel_pass() is False  # and no LLM pass spent on a dead token
    assert store.count_pending_inbox() == 0
