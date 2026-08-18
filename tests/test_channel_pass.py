"""The channel pass: prompt building with the transcript window, coalesced routing, the fold on
settle (handled / failed + notice, no auto-refire — off durable rows, so a stale-swept crash
folds too), and the typing pulse.
"""

from __future__ import annotations

import threading
import time

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from sellee import passes, secrets
from sellee.channel import outbound
from sellee.channel.prompt import (
    TRANSCRIPT_CHAR_CAP,
    _format_transcript,
    build_channel_prompt,
)
from sellee.channel.telegram.poller import Poller
from sellee.channel.telegram.transport import TelegramClient
from sellee.config import Config
from sellee.proc_tree import PASS_PROMPT_MARKER
from sellee.tools import TIER_PASS_CHANNEL, tools_for_tier


def _bound(store):
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", "n1")
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


def test_the_window_keeps_a_photo_reachable_after_its_own_pass_ended() -> None:
    """The failure this prevents: the photo arrives in one pass, the price is agreed in the next,
    and the second pass drafts a listing with no photo because the path was only ever in the first
    pass's pending block."""
    transcript = [
        {"direction": "in", "kind": "photo", "text": "list this", "media_paths": ["/m/a.jpg"]},
        {"direction": "out", "kind": "notice", "text": "How does $5 sound?", "ts": 2.0},
    ]
    prompt = build_channel_prompt([{"kind": "text", "text": "yes", "media_paths": []}], transcript)
    assert "/m/a.jpg" in prompt


def test_the_window_does_not_repeat_a_photo_the_pending_block_already_lists() -> None:
    """One authoritative copy: the same path in both blocks reads as two photos to attach."""
    row = {"kind": "photo", "text": "list this", "media_paths": ["/m/a.jpg"]}
    transcript = [
        {"direction": "in", "kind": "photo", "text": "list this", "media_paths": ["/m/a.jpg"]}
    ]
    assert build_channel_prompt([row], transcript).count("/m/a.jpg") == 1


def test_prompt_photo_rows_carry_their_stored_paths() -> None:
    """The paths are the usable part: they are already in the media store, so a listing flow can
    put them straight onto an item. A count alone would tell the pass photos exist and leave it
    with no way to reach them."""
    claimed = [{"kind": "photo", "text": "sell this", "media_paths": ["/m/a.jpg", "/m/b.jpg"]}]
    prompt = build_channel_prompt(claimed, [])
    assert "[2 photo(s)] sell this" in prompt
    assert "/m/a.jpg" in prompt
    assert "/m/b.jpg" in prompt


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


# --- fold on settle ---------------------------------------------------------------------------


def test_fold_handled_on_ok(store, bus, xdg_tmp) -> None:
    _bound(store)
    store.ingest_updates([_ev(1, text="hi")], update_offset=2)
    pass_id = store.enqueue_channel_pass()
    store.claim_queued_pass()
    store.finish_pass(pass_id, status="done", cls="ok")
    outbound.fold_settled_passes(store=store)
    rows = store.inbox_for_pass(pass_id)
    assert rows[0]["status"] == "handled"
    assert store.count_queued_notices() == 0  # no notice on success


def test_fold_failed_queues_one_notice_no_refire(store, bus, xdg_tmp) -> None:
    _bound(store)
    store.ingest_updates([_ev(1, text="hi")], update_offset=2)
    pass_id = store.enqueue_channel_pass()
    store.claim_queued_pass()
    store.finish_pass(pass_id, status="error", cls="error")
    outbound.fold_settled_passes(store=store)
    rows = store.inbox_for_pass(pass_id)
    assert rows[0]["status"] == "failed"  # terminal, never re-claimed
    notices = store.list_queued_notices()
    assert len(notices) == 1  # one loud notice, traceable to the pass
    assert notices[0]["pass_id"] == pass_id
    # idempotent: another lane tick folds nothing and queues no second notice
    outbound.fold_settled_passes(store=store)
    assert store.count_queued_notices() == 1
    # a failed row is never picked up by a new pass
    assert store.enqueue_channel_pass() is None


def test_fold_leaves_a_running_pass_alone(store, bus, xdg_tmp) -> None:
    _bound(store)
    store.ingest_updates([_ev(1, text="hi")], update_offset=2)
    pass_id = store.enqueue_channel_pass()
    store.claim_queued_pass()  # running, not settled
    outbound.fold_settled_passes(store=store)
    assert store.inbox_for_pass(pass_id)[0]["status"] == "claimed"
    assert store.count_queued_notices() == 0


def test_stale_swept_pass_still_folds_failed(store, bus, xdg_tmp) -> None:
    """The crash shape: the daemon dies mid-channel-pass, restarts, and the stale-running sweep
    fails the row. The fold lane must still fold the claimed messages and queue the failure
    notice — this used to ride a pass.end event the sweep never emitted in the right shape,
    leaving the seller's messages claimed forever."""
    _bound(store)
    store.ingest_updates([_ev(1, text="hi")], update_offset=2)
    pass_id = store.enqueue_channel_pass()
    store.claim_queued_pass()  # running — then the daemon "dies"
    assert store.fail_stale_running(0, now=time.time() + 10_000) == [pass_id]
    outbound.fold_settled_passes(store=store)
    rows = store.inbox_for_pass(pass_id)
    assert rows[0]["status"] == "failed"
    assert store.count_queued_notices() == 1


# --- typing pulse ---------------------------------------------------------------------------


def test_typing_pulse_only_while_a_channel_pass_is_active(store, bus, xdg_tmp) -> None:
    _bound(store)
    with FakeTelegramAPI() as api:

        def typing(chat_id):
            TelegramClient(FAKE_TOKEN, api_base=api.base_url).send_chat_action(chat_id, "typing")

        # no active channel pass -> no pulse
        outbound.pulse_typing(store=store, typing=typing)
        assert api.chat_actions == []
        # with a channel pass queued -> a typing action goes out
        store.ingest_updates([_ev(1, text="hi")], update_offset=2)
        store.enqueue_channel_pass()
        outbound.pulse_typing(store=store, typing=typing)
        assert api.chat_actions == [CHAT_ID]
