"""get_catchup and the status surfacing: the needs-me read lists open escalations and queued
notices, stamps the returned notices delivered-via-catchup (escalations are never stamped by a
read), and computes the connect hint at read time. get_status carries channel/pause/notice state.
"""

from __future__ import annotations

import time

from selly_agent.tools import TIER_ATTENDED
from selly_agent.tools.registry import dispatch


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
    import selly_agent.store as store_mod

    # stamp the escalation 25h in the past (older than the 24h connect-hint threshold)
    monkeypatch.setattr(store_mod, "_now", lambda: time.time() - 25 * 3600)
    store.escalate("carousell:t1", open_question="old question")
    monkeypatch.setattr(store_mod, "_now", time.time)

    ctx = make_ctx(TIER_ATTENDED)
    assert dispatch("get_catchup", {}, ctx)["connect_hint"] is True  # unbound + aged

    # once bound, the hint never fires (the escalation can be pushed instead)
    store.arm_bind("selly_test_bot", "n1")
    store.complete_bind(999, update_offset=1)
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
    store.arm_bind("selly_test_bot", "n1")
    store.complete_bind(999, update_offset=1)
    status = dispatch("get_status", {}, ctx)
    assert status["channel_bound"] is True and status["paused"] is True
