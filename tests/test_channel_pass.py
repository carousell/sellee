"""The channel pass: prompt building with the transcript window, coalesced routing, the fold on
end (handled / failed + notice, no auto-refire), and the typing pulse.
"""

from __future__ import annotations

import threading

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from selly_agent import passes, secrets
from selly_agent.channel import delivery
from selly_agent.channel.poller import Poller
from selly_agent.channel.prompt import (
    TRANSCRIPT_CHAR_CAP,
    _format_transcript,
    build_channel_prompt,
)
from selly_agent.channel.telegram import TelegramClient
from selly_agent.config import Config
from selly_agent.proc_tree import PASS_PROMPT_MARKER
from selly_agent.tools import TIER_PASS_CHANNEL, tools_for_tier


def _bound(store):
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1)


def _poller(store, bus, api):
    return Poller(
        store=store,
        config=Config(telegram_api_base=api.base_url),
        bus=bus,
        stop_event=threading.Event(),
        client_factory=lambda token: TelegramClient(FAKE_TOKEN, api_base=api.base_url),
        poll_timeout=0,
    )


def _ev(uid, kind="text", text="hi", **payload):
    return {"event_id": uid, "kind": kind, "text": text, "payload": payload, "src_ts": 1.0}


# --- prompt building ------------------------------------------------------------------------


def test_prompt_separates_history_from_work() -> None:
    transcript = [
        {"direction": "in", "kind": "text", "text": "how much for the lamp?", "ts": 1.0},
        {"direction": "out", "kind": "notice", "text": "It's $80.", "ts": 2.0},
        {"direction": "in", "kind": "text", "text": "would you take 70?", "ts": 3.0},
    ]
    claimed = [{"kind": "text", "text": "would you take 70?", "media_paths": []}]
    prompt = build_channel_prompt(claimed, transcript)
    assert prompt.startswith(PASS_PROMPT_MARKER)
    assert "Recent conversation" in prompt
    assert "[seller] how much for the lamp?" in prompt
    assert "[you] It's $80." in prompt  # the agent's own notice is in the window (memory)
    assert "Messages to handle now:" in prompt
    assert "1. would you take 70?" in prompt


def test_prompt_photo_rows_summarized() -> None:
    claimed = [{"kind": "photo", "text": "sell this", "media_paths": ["/m/a.jpg", "/m/b.jpg"]}]
    prompt = build_channel_prompt(claimed, [])
    assert "[2 photo(s)] sell this" in prompt


def test_transcript_window_trims_to_char_cap() -> None:
    entries = [
        {"direction": "in", "kind": "text", "text": "x" * 100, "ts": float(i)} for i in range(200)
    ]
    window = _format_transcript(entries, TRANSCRIPT_CHAR_CAP)
    assert len(window) <= TRANSCRIPT_CHAR_CAP
    # the most recent entries survive (oldest dropped first)
    assert window.endswith("x" * 100)


# --- routing & coalescing --------------------------------------------------------------------


def test_freetext_routes_one_coalesced_pass(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_text("is the lamp available?")
        api.inject_text("and the chair?")
        _poller(store, bus, api).tick()
    # one channel pass claims both pending rows
    assert store.has_active_channel_pass() is True
    queued = [e for e in bus.store.read() if e.kind == "pass.queued"]
    assert len(queued) == 1 and queued[0].payload["type"] == "channel"
    assert store.count_pending_inbox() == 0


def test_second_batch_waits_while_a_pass_is_active(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:
        api.inject_text("first")
        _poller(store, bus, api).tick()
        api.inject_text("second")  # arrives while the first pass is still queued
        _poller(store, bus, api).tick()
    queued = [e for e in bus.store.read() if e.kind == "pass.queued"]
    assert len(queued) == 1  # coalesced — no second pass
    assert store.count_pending_inbox() == 1  # the second message waits


# --- the pass carries a full-scope, pass:channel token ---------------------------------------


def test_channel_pass_prompt_and_tier(store, bus, xdg_tmp) -> None:
    _bound(store)
    # seed a prior turn so the window has memory, then the current message
    store.queue_notice("It's $80.")
    store.ingest_updates([_ev(1, text="would you take 70?")], update_offset=2)
    pass_id = store.enqueue_channel_pass()
    prompt = passes._channel_prompt({"inbox_ids": []}, store, pass_id)
    assert "would you take 70?" in prompt and "It's $80." in prompt
    # the tier's tool set is the pass:channel surface (full-scope sell conversation)
    names = {s.name for s in tools_for_tier(TIER_PASS_CHANNEL)}
    assert {"create_item", "set_floor", "send_message", "escalate"} <= names


# --- fold on end ----------------------------------------------------------------------------


def test_fold_handled_on_ok(store, bus, xdg_tmp) -> None:
    _bound(store)
    store.ingest_updates([_ev(1, text="hi")], update_offset=2)
    pass_id = store.enqueue_channel_pass()
    fold = delivery.channel_pass_folder(store)

    class _E:
        kind = "pass.end"
        payload = {"type": "channel", "class": "ok"}

    e = _E()
    e.pass_id = pass_id
    fold(e)
    rows = store.inbox_for_pass(pass_id)
    assert rows[0]["status"] == "handled"
    assert store.count_queued_notices() == 0  # no notice on success


def test_fold_failed_queues_one_notice_no_refire(store, bus, xdg_tmp) -> None:
    _bound(store)
    store.ingest_updates([_ev(1, text="hi")], update_offset=2)
    pass_id = store.enqueue_channel_pass()
    fold = delivery.channel_pass_folder(store)

    class _E:
        kind = "pass.end"
        payload = {"type": "channel", "class": "error"}

    e = _E()
    e.pass_id = pass_id
    fold(e)
    rows = store.inbox_for_pass(pass_id)
    assert rows[0]["status"] == "failed"  # terminal, never re-claimed
    assert store.count_queued_notices() == 1  # one loud notice
    # a failed row is never picked up by a new pass
    assert store.enqueue_channel_pass() is None


# --- typing pulse ---------------------------------------------------------------------------


def test_typing_pulse_only_while_a_channel_pass_is_active(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:

        def cf(token):
            return TelegramClient(FAKE_TOKEN, api_base=api.base_url)

        # no active channel pass -> no pulse
        delivery.pulse_typing(store=store, config=Config(), client_factory=cf)
        assert api.chat_actions == []
        # with a channel pass queued -> a typing action goes out
        store.ingest_updates([_ev(1, text="hi")], update_offset=2)
        store.enqueue_channel_pass()
        delivery.pulse_typing(store=store, config=Config(), client_factory=cf)
        assert api.chat_actions == [CHAT_ID]
