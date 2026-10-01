"""The relay reply sink: send_reply answering a carousell.ai email thread through bazaar's
reply_to_thread, against a fake of bazaar's seller MCP server."""

from __future__ import annotations

import time

import pytest
from tests.fake_carousell_ai_mcp import FakeRelay, serve

from sellee.config import Config
from sellee.rail import inbox as relay
from sellee.rail.client import RailClient
from sellee.rail.sink import RelayReplySink
from sellee.tools.registry import dispatch

_WEB = "https://www.carousell.ai"
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
_THREAD = "carousell-ai:t1"


@pytest.fixture
def fake():
    relay_state = FakeRelay()
    base, shutdown = serve(relay_state)
    relay_state.base = base
    yield relay_state
    shutdown()


@pytest.fixture
def waiting(store, bus, fake):
    """A buyer's email imported by the relay lane, waiting on an answer."""
    made = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(made["id"], "carousell-ai", f"{_WEB}/listing/L1")
    fake.add_thread("t1", listing_id="L1")
    fake.add_message("t1", "m1", "buyer", "Is the lamp still available?")
    relay.relay_lane(_deps(store, bus, fake))
    return fake


def _client(fake, timeout_sec=5.0):
    return RailClient(api_base=fake.base, api_key="k", web_base_url=_WEB, timeout_sec=timeout_sec)


def _deps(store, bus, fake):
    return relay.RelayDeps(
        store=store, bus=bus, config=Config(), rail_factory=lambda: _client(fake)
    )


def _send(make_ctx, store, bus, fake, *, timeout_sec=5.0):
    sink = RelayReplySink(
        client=_client(fake, timeout_sec), store=store, bus=bus, retry_delays_sec=(0, 0, 0)
    )
    ctx = make_ctx("attended", reply_sink=sink, config=_FAST)
    return dispatch("send_reply", {"thread_id": _THREAD, "text": "Yes, still here!"}, ctx)


def _waiting_threads(store) -> set:
    return {row["thread_id"] for row in store.threads_with_unhandled_inbound()}


def _intent_statuses(store) -> list:
    return [row["status"] for row in store._db.query("SELECT status FROM send_intents")]


def _outbound(store) -> list:
    return [m["msg_id"] for m in store.get_thread(_THREAD)["messages"] if m["dir"] == "out"]


def test_a_waiting_thread_is_answered_with_one_reply_to_thread_call(make_ctx, store, bus, waiting):
    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "sent"
    assert [c["id"] for c in waiting.reply_calls] == ["t1"]
    assert waiting.reply_calls[0]["client_message_id"] == res["intent_id"]
    assert _waiting_threads(store) == set()
    # Recorded under bazaar's id, so the lane's next read of the thread adds nothing.
    assert _outbound(store) == ["r1"] and res["msg_id"] == "r1"
    relay.relay_lane(_deps(store, bus, waiting))
    assert _outbound(store) == ["r1"]


@pytest.mark.parametrize("failure", ["http503", "http429", "internal", "busy"])
def test_a_5xx_503_or_429_is_retried_with_the_same_id(make_ctx, store, bus, waiting, failure):
    waiting.reply_script = [failure]

    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "sent"
    ids = {c["client_message_id"] for c in waiting.reply_calls}
    assert len(waiting.reply_calls) == 2 and ids == {res["intent_id"]}
    assert [m["id"] for m in waiting.messages["t1"] if m["author"] == "agent"] == ["r1"]


def test_a_success_without_a_message_id_is_retried_not_refused(make_ctx, store, bus, waiting):
    waiting.reply_script = ["no_id"]

    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "sent"
    assert len(waiting.reply_calls) == 2
    assert len([m for m in waiting.messages["t1"] if m["author"] == "agent"]) == 1


def test_a_refusal_after_an_uncertain_attempt_keeps_the_intent(make_ctx, store, bus, waiting):
    """The first attempt may have stored the reply, so a later refusal proves nothing about it."""
    waiting.reply_script = ["busy"]
    original = waiting.reply_to_thread

    def block_after_first(args):
        if waiting.reply_calls:
            waiting.threads["t1"]["buyer_blocked"] = True
        return original(args)

    waiting.reply_to_thread = block_after_first

    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "send_unverified"
    assert _intent_statuses(store) == ["sent_unverified"]
    assert len(waiting.reply_calls) == 2


def test_a_timeout_is_retried_with_the_same_id(make_ctx, store, bus, waiting):
    waiting.reply_script = ["slow"]
    waiting.slow_sec = 0.6

    res = _send(make_ctx, store, bus, waiting, timeout_sec=0.2)

    assert res["status"] == "sent"
    assert len({c["client_message_id"] for c in waiting.reply_calls}) == 1
    assert len(waiting.reply_calls) == 2
    assert len([m for m in waiting.messages["t1"] if m["author"] == "agent"]) == 1


def test_a_blocked_buyer_fails_the_send_without_a_retry(make_ctx, store, bus, waiting):
    waiting.threads["t1"]["buyer_blocked"] = True

    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "send_failed" and res["delivered"] == "no"
    assert len(waiting.reply_calls) == 1
    assert _intent_statuses(store) == []
    # Nothing is left for the stale-send sweep to ask the seller about.
    assert store.stale_intent_sweep(grace_sec=0, now=4_000_000_000.0) == []


def test_a_send_still_failing_after_retries_is_unverified_until_bazaar_shows_it(
    make_ctx, store, bus, waiting
):
    waiting.reply_script = ["busy", "busy", "busy", "busy"]

    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "send_unverified"
    assert _intent_statuses(store) == ["sent_unverified"]
    # bazaar holds the reply but has not sent it yet, so the buyer is not answered.
    relay.relay_lane(_deps(store, bus, waiting))
    assert store.get_thread(_THREAD)["cursor_last_msg_id"] is None
    assert _intent_statuses(store) == ["sent_unverified"]

    waiting.finish_send("t1", "r1")
    relay.relay_lane(_deps(store, bus, waiting))

    assert _intent_statuses(store) == ["committed"]
    assert store.get_thread(_THREAD)["cursor_last_msg_id"] == "m1"
    assert _waiting_threads(store) == set()
    assert _outbound(store) == ["r1"]


def test_a_relay_reply_is_not_held_by_the_browser_pacing_cap(make_ctx, store, bus, waiting):
    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "sent"
    rows = store._db.query("SELECT 1 FROM pacing_actions WHERE marketplace = ?", ("carousell-ai",))
    assert rows == []


def _later(store, bus, fake, after_sec):
    """A relay lane tick `after_sec` after now."""
    deps = _deps(store, bus, fake)
    deps.now = lambda: time.time() + after_sec
    relay.relay_lane(deps)


def _stuck(make_ctx, store, bus, fake):
    """A send whose every attempt failed before bazaar stored anything."""
    fake.reply_script = ["http503"] * 4
    res = _send(make_ctx, store, bus, fake)
    assert res["status"] == "send_unverified" and fake.messages["t1"][-1]["author"] == "buyer"
    return res["intent_id"]


def test_the_lane_finishes_a_send_no_attempt_got_through(make_ctx, store, bus, waiting):
    intent = _stuck(make_ctx, store, bus, waiting)

    _later(store, bus, waiting, relay.RETRY_SEND_AFTER_SEC + 1)

    assert waiting.reply_calls[-1]["client_message_id"] == intent
    assert len([m for m in waiting.messages["t1"] if m["author"] == "agent"]) == 1
    assert _intent_statuses(store) == ["committed"]
    assert _outbound(store) == ["r1"]
    assert _waiting_threads(store) == set()


def test_the_lane_leaves_a_send_still_in_flight(make_ctx, store, bus, waiting):
    _stuck(make_ctx, store, bus, waiting)
    calls = len(waiting.reply_calls)

    _later(store, bus, waiting, 1)

    assert len(waiting.reply_calls) == calls
    assert _intent_statuses(store) == ["sent_unverified"]


def test_the_lane_leaves_a_send_bazaar_is_already_retrying(make_ctx, store, bus, waiting):
    waiting.reply_script = ["busy"] * 4
    _send(make_ctx, store, bus, waiting)
    calls = len(waiting.reply_calls)

    _later(store, bus, waiting, relay.RETRY_SEND_AFTER_SEC + 1)

    assert len(waiting.reply_calls) == calls
    assert _intent_statuses(store) == ["sent_unverified"]


def test_a_retry_bazaar_refuses_drops_the_send(make_ctx, store, bus, waiting):
    _stuck(make_ctx, store, bus, waiting)
    waiting.threads["t1"]["buyer_blocked"] = True

    _later(store, bus, waiting, relay.RETRY_SEND_AFTER_SEC + 1)

    assert _intent_statuses(store) == []


def test_the_sweep_waits_a_day_before_asking_about_a_relay_send(make_ctx, store, bus, waiting):
    _stuck(make_ctx, store, bus, waiting)
    now = time.time()

    assert store.stale_intent_sweep(grace_sec=600, now=now + 7200) == []
    folded = store.stale_intent_sweep(grace_sec=600, now=now + 2 * 86400)

    assert len(folded) == 1
    question = store._db.query("SELECT open_question FROM escalations")[0]["open_question"]
    assert "email" in question and "app" not in question
