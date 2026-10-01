"""The relay lane: carousell.ai email threads imported through the rail's list_threads and
get_thread, against a fake of bazaar's seller MCP server."""

from __future__ import annotations

import random

import pytest
from tests.fake_carousell_ai_mcp import FakeRelay, serve

from sellee.config import Config
from sellee.rail import inbox as relay
from sellee.rail.client import RailClient

_WEB = "https://www.carousell.ai"


@pytest.fixture
def fake():
    relay_state = FakeRelay()
    base, shutdown = serve(relay_state)
    relay_state.base = base
    yield relay_state
    shutdown()


@pytest.fixture
def item(store):
    made = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(made["id"], "carousell-ai", f"{_WEB}/listing/L1")
    return store.get_item(made["id"])


def _deps(store, bus, fake):
    return relay.RelayDeps(
        store=store,
        bus=bus,
        config=Config(),
        rail_factory=lambda: RailClient(api_base=fake.base, api_key="k", web_base_url=_WEB),
    )


def _waiting(store) -> set:
    return {row["thread_id"] for row in store.threads_with_unhandled_inbound()}


def test_a_buyer_message_imports_as_a_waiting_thread_on_the_item(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Is the lamp still available?")

    relay.relay_lane(_deps(store, bus, fake))

    thread = store.get_thread("carousell-ai:t1")
    assert thread is not None
    assert (thread["side"], thread["market"], thread["item_id"]) == (
        "sell",
        "carousell-ai",
        item["id"],
    )
    assert thread["counterpart_handle"] == "t1@reply.carousell.ai"
    assert [(m["msg_id"], m["dir"]) for m in thread["messages"]] == [("m1", "in")]
    assert _waiting(store) == {"carousell-ai:t1"}


def test_a_thread_on_a_listing_not_ours_is_left_alone(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L-other")
    fake.add_message("t1", "m1", "buyer", "hi")

    relay.relay_lane(_deps(store, bus, fake))

    assert store.get_thread("carousell-ai:t1") is None


def test_a_crash_before_the_cursor_is_stored_doubles_nothing(store, bus, fake, item, monkeypatch):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Still there?")
    deps = _deps(store, bus, fake)
    real = store.set_relay_cursor
    monkeypatch.setattr(store, "set_relay_cursor", _raise)
    with pytest.raises(RuntimeError):
        relay.relay_lane(deps)
    monkeypatch.setattr(store, "set_relay_cursor", real)
    relay.relay_lane(deps)
    relay.relay_lane(deps)

    assert [m["msg_id"] for m in store.get_thread("carousell-ai:t1")["messages"]] == ["m1"]
    assert store.get_relay_cursor() != ""


@pytest.mark.parametrize("author", ["seller", "agent"])
def test_a_thread_last_answered_by_our_side_imports_as_answered(store, bus, fake, item, author):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Would you take 60?")
    fake.add_message("t1", "m2", author, "70 is my lowest.", client_id="c1")

    relay.relay_lane(_deps(store, bus, fake))

    messages = store.get_thread("carousell-ai:t1")["messages"]
    assert [(m["msg_id"], m["dir"]) for m in messages] == [("m1", "in"), ("m2", "out")]
    assert _waiting(store) == set()


def test_a_buyer_writing_again_after_our_reply_is_waiting_again(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Would you take 60?")
    fake.add_message("t1", "m2", "agent", "70 is my lowest.", client_id="c1")
    deps = _deps(store, bus, fake)
    relay.relay_lane(deps)
    fake.add_message("t1", "m3", "buyer", "OK, 70 then.")

    relay.relay_lane(deps)

    assert _waiting(store) == {"carousell-ai:t1"}


def test_a_blocked_buyer_closes_the_thread(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "hi")
    deps = _deps(store, bus, fake)
    relay.relay_lane(deps)
    fake.block("t1")

    relay.relay_lane(deps)

    assert store.get_thread("carousell-ai:t1")["status"] == "closed"
    assert _waiting(store) == set()


def test_a_thread_first_seen_blocked_imports_closed(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "hi")
    fake.block("t1")

    relay.relay_lane(_deps(store, bus, fake))

    assert store.get_thread("carousell-ai:t1")["status"] == "closed"


def test_the_rail_down_for_a_tick_raises_nothing_and_the_next_catches_up(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "hi")
    deps = _deps(store, bus, fake)
    fake.down = True

    relay.relay_lane(deps)

    assert store.get_thread("carousell-ai:t1") is None
    fake.down = False
    relay.relay_lane(deps)
    assert _waiting(store) == {"carousell-ai:t1"}


def test_an_unprovisioned_rail_is_skipped_quietly(store, bus, item):
    def unprovisioned():
        from sellee.rail.client import RailUnprovisioned

        raise RailUnprovisioned("carousell.ai is not provisioned")

    relay.relay_lane(
        relay.RelayDeps(store=store, bus=bus, config=Config(), rail_factory=unprovisioned)
    )

    assert store.get_relay_cursor() == ""


def test_the_cursor_pages_through_more_threads_than_one_page(store, bus, fake, item, monkeypatch):
    monkeypatch.setattr(relay, "PAGE_SIZE", 2)
    for n in range(5):
        fake.add_thread(f"t{n}", listing_id="L1", email=f"t{n}@reply.carousell.ai")
        fake.add_message(f"t{n}", f"m{n}", "buyer", "hi")

    relay.relay_lane(_deps(store, bus, fake))

    assert _waiting(store) == {f"carousell-ai:t{n}" for n in range(5)}


def test_property_every_message_is_stored_once_across_crashes_and_refetches(
    store, bus, fake, item, monkeypatch
):
    """Property: over random writes, polls and crashes before the cursor is stored, each bazaar
    message ends up in the transcript exactly once, in bazaar's order."""
    rng = random.Random(7)
    deps = _deps(store, bus, fake)
    real = store.set_relay_cursor
    sent: dict = {}
    for _ in range(150):
        roll = rng.random()
        if roll < 0.15 or not fake.threads:
            tid = f"t{len(fake.threads)}"
            fake.add_thread(tid, listing_id="L1", email=f"{tid}@reply.carousell.ai")
            sent[tid] = []
        if roll < 0.6:
            tid = rng.choice(sorted(sent))
            mid = f"{tid}-m{len(sent[tid])}"
            fake.add_message(tid, mid, rng.choice(["buyer", "buyer", "seller", "agent"]), mid)
            sent[tid].append(mid)
        else:
            crash = rng.random() < 0.3
            if crash:
                monkeypatch.setattr(store, "set_relay_cursor", _raise)
            try:
                relay.relay_lane(deps)
            except RuntimeError:
                pass
            monkeypatch.setattr(store, "set_relay_cursor", real)
    relay.relay_lane(deps)

    for tid, mids in sent.items():
        if not mids:
            continue
        stored = [m["msg_id"] for m in store.get_thread(f"carousell-ai:{tid}")["messages"]]
        assert stored == mids, tid


def _raise(_cursor):
    raise RuntimeError("killed before the cursor was stored")


def test_a_thread_whose_item_is_linked_later_is_imported_then(store, bus, fake):
    made = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Is it still available?")
    fake.repeat_tail = False  # polled past bazaar's 30-second overlap
    deps = _deps(store, bus, fake)
    relay.relay_lane(deps)
    relay.relay_lane(deps)
    assert store.get_thread("carousell-ai:t1") is None

    store.record_listing_url(made["id"], "carousell-ai", f"{_WEB}/listing/L1")
    relay.relay_lane(deps)

    assert store.get_thread("carousell-ai:t1")["item_id"] == made["id"]
    assert _waiting(store) == {"carousell-ai:t1"}


# bazaar owns a reply once stored, so it answers the buyer before it is sent.
def test_an_agent_reply_bazaar_is_still_sending_answers_the_buyer(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Would you take 60?")
    fake.add_message("t1", "m2", "agent", "70 is my lowest.", pending_send=True, client_id="c1")

    relay.relay_lane(_deps(store, bus, fake))

    assert _waiting(store) == set()
    assert store.relay_rereads() == []


def test_a_block_on_a_held_thread_closes_it_once_released(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "hi")
    fake.repeat_tail = False  # polled past bazaar's 30-second overlap
    deps = _deps(store, bus, fake)
    relay.relay_lane(deps)
    store.hold_thread("carousell-ai:t1", "seller asked to wait")
    fake.block("t1")
    relay.relay_lane(deps)
    relay.relay_lane(deps)
    assert store.get_thread("carousell-ai:t1")["status"] == "held"

    store.release_thread("carousell-ai:t1")
    relay.relay_lane(deps)

    assert store.get_thread("carousell-ai:t1")["status"] == "closed"


def test_a_thread_that_fails_to_read_holds_back_no_other_thread(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "first")
    fake.add_thread("t2", listing_id="L1", email="t2@reply.carousell.ai")
    fake.add_message("t2", "m2", "buyer", "second")
    fake.broken.add("t1")
    fake.repeat_tail = False  # polled past bazaar's 30-second overlap
    deps = _deps(store, bus, fake)

    relay.relay_lane(deps)

    assert [m["msg_id"] for m in store.get_thread("carousell-ai:t2")["messages"]] == ["m2"]
    fake.broken.clear()
    relay.relay_lane(deps)
    relay.relay_lane(deps)
    assert [m["msg_id"] for m in store.get_thread("carousell-ai:t1")["messages"]] == ["m1"]


def test_the_reply_lane_claims_relay_threads(store, bus, fake, item, monkeypatch):
    from sellee.browser import inbox as browser_inbox

    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Is it available?")
    relay.relay_lane(_deps(store, bus, fake))
    skipped = []

    def enqueue(skip_markets=()):
        skipped.extend(skip_markets)
        return None

    monkeypatch.setattr(store, "enqueue_reply_pass", enqueue)
    browser_inbox.reply_lane(store=store, bus=bus, config=Config())

    assert "carousell-ai" not in skipped


def _set_status(store, thread_id, status):
    with store._db.transaction() as conn:
        conn.execute("UPDATE threads SET status = ? WHERE thread_id = ?", (status, thread_id))


@pytest.mark.parametrize("status", ["liaising", "agreed"])
def test_a_block_closes_a_thread_past_active(store, bus, fake, item, status):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Deal at 70.")
    deps = _deps(store, bus, fake)
    relay.relay_lane(deps)
    _set_status(store, "carousell-ai:t1", status)
    fake.block("t1")

    relay.relay_lane(deps)

    assert store.get_thread("carousell-ai:t1")["status"] == "closed"


def test_a_block_on_a_held_negotiation_closes_it_once_released(store, bus, fake, item):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Deal at 70.")
    fake.repeat_tail = False  # polled past bazaar's 30-second overlap
    deps = _deps(store, bus, fake)
    relay.relay_lane(deps)
    _set_status(store, "carousell-ai:t1", "liaising")
    store.hold_thread("carousell-ai:t1", "seller asked to wait")
    fake.block("t1")
    relay.relay_lane(deps)
    assert store.get_thread("carousell-ai:t1")["status"] == "held"

    store.release_thread("carousell-ai:t1")
    relay.relay_lane(deps)

    assert store.get_thread("carousell-ai:t1")["status"] == "closed"


def test_a_hold_landing_mid_read_keeps_the_block_pending(store, bus, fake, item, monkeypatch):
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "hi")
    fake.repeat_tail = False  # polled past bazaar's 30-second overlap
    deps = _deps(store, bus, fake)
    relay.relay_lane(deps)
    fake.block("t1")
    record = relay._record_messages

    def hold_mid_read(*args):
        owed = record(*args)
        store.hold_thread("carousell-ai:t1", "seller asked to wait")
        return owed

    monkeypatch.setattr(relay, "_record_messages", hold_mid_read)
    relay.relay_lane(deps)
    monkeypatch.setattr(relay, "_record_messages", record)
    assert store.get_thread("carousell-ai:t1")["status"] == "held"

    store.release_thread("carousell-ai:t1")
    relay.relay_lane(deps)

    assert store.get_thread("carousell-ai:t1")["status"] == "closed"
