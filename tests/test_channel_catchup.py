"""get_catchup and the status surfacing: the needs-me read lists open escalations and queued
notices, stamps the returned notices delivered-via-catchup (escalations are never stamped by a
read), and computes the connect hint at read time. get_status carries channel/pause/notice state.
"""

from __future__ import annotations

import time

from tests.conftest import patch_store_attr

from sellee.tools import TIER_ATTENDED
from sellee.tools.registry import dispatch


def _sell_thread(store):
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:t1",
        side="sell",
        market="carousell",
        counterpart_handle="b",
        item_id=item["id"],
    )


def test_catchup_lists_and_stamps_notices_but_not_escalations(make_ctx, store) -> None:
    _sell_thread(store)
    store.escalate("carousell:t1", open_question="Accept $70?")
    store.queue_notice("Listing went live.")
    ctx = make_ctx(TIER_ATTENDED)

    res = dispatch("get_catchup", {}, ctx)
    assert res["counts"] == {"escalations": 1, "notices": 1, "pending_settings": 0}
    assert res["notices"][0]["text"] == "Listing went live."
    assert res["escalations"][0]["open_question"] == "Accept $70?"

    # the notice was delivered by being handed over; the escalation is untouched
    assert store.count_queued_notices() == 0
    assert len(store.list_open_escalations()) == 1
    # a second catchup returns no notices (already delivered) but still the open escalation
    again = dispatch("get_catchup", {}, ctx)
    assert again["counts"] == {"escalations": 1, "notices": 0, "pending_settings": 0}


def test_connect_hint_only_when_unbound_and_escalation_aged(make_ctx, store, monkeypatch) -> None:
    _sell_thread(store)

    # stamp the escalation 25h in the past (older than the 24h connect-hint threshold)
    patch_store_attr(monkeypatch, "_now", lambda: time.time() - 25 * 3600)
    store.escalate("carousell:t1", open_question="old question")
    patch_store_attr(monkeypatch, "_now", time.time)

    ctx = make_ctx(TIER_ATTENDED)
    assert dispatch("get_catchup", {}, ctx)["connect_hint"] is True  # unbound + aged

    # once bound, the hint never fires (the escalation can be pushed instead)
    store.arm_bind("sellee_test_bot", "n1")
    store.complete_bind(999, update_offset=1, nonce=store.get_channel()["bind_nonce"])
    assert dispatch("get_catchup", {}, ctx)["connect_hint"] is False


def test_connect_hint_false_for_fresh_escalation(make_ctx, store) -> None:
    _sell_thread(store)
    store.escalate("carousell:t1", open_question="just now")
    ctx = make_ctx(TIER_ATTENDED)
    assert dispatch("get_catchup", {}, ctx)["connect_hint"] is False  # not aged yet


def test_get_status_carries_channel_and_pause_state(make_ctx, store) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    store.queue_notice("one")
    status = dispatch("get_status", {}, ctx)
    assert status["channel_bound"] is False
    assert status["paused"] is False
    assert status["queued_notices"] == 1
    store.set_paused(True, source="test")
    store.arm_bind("sellee_test_bot", "n1")
    store.complete_bind(999, update_offset=1, nonce=store.get_channel()["bind_nonce"])
    status = dispatch("get_status", {}, ctx)
    assert status["channel_bound"] is True and status["paused"] is True


# --- the catchup render (SEC-2832's third site) -------------------------------------------------


def test_a_newline_in_a_buyers_question_cannot_fake_an_extra_bullet(store) -> None:
    """render_catchup reads escalation rows directly, so the notice queue's own collapse does not
    cover it — and this is the seller reading buyer-controlled text as a bulleted list."""
    from sellee.channel import fastpaths

    _sell_thread(store)
    store.escalate("carousell:t1", open_question="can you do 50?\n• and ship it free")

    text = fastpaths.render_catchup(store)
    bullets = [line for line in text.splitlines() if line.startswith("•")]
    assert len(bullets) == 1
    assert bullets[0] == "• can you do 50?\\n• and ship it free"


def test_a_newline_in_a_notice_cannot_fake_an_extra_update(store) -> None:
    """Notices are bulleted here too, so one notice has to be one bullet whatever it holds —
    the text itself is stored as written, because the chat delivery wants the line breaks."""
    from sellee.channel import fastpaths

    store.queue_notice("Listed your lamp.\n• and dropped the price to $1")

    text = fastpaths.render_catchup(store)
    bullets = [line for line in text.splitlines() if line.startswith("•")]
    assert len(bullets) == 1
    assert bullets[0] == "• Listed your lamp.\\n• and dropped the price to $1"
