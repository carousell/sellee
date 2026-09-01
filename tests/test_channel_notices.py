"""Notices: the drain lane delivers to a bound chat FIFO and stamps them; unbound or paused it is
a no-op (catchup delivers instead); a transport failure bumps attempts and re-raises so the
scheduler backs off. The escalation-push subscriber queues exactly one notice per escalation.
"""

from __future__ import annotations

import pytest

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from sellee import secrets
from sellee.channel import outbound
from sellee.channel.telegram.transport import ChannelError, TelegramClient
from sellee.tools import TIER_ATTENDED
from sellee.tools.registry import dispatch


def _bind(store):
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1, nonce=store.get_channel()["bind_nonce"])


def _deliver(api):
    def deliver(chat_id, text, controls=None):
        TelegramClient(FAKE_TOKEN, api_base=api.base_url).send_message(chat_id, text)

    return deliver


def _drain(store, bus, api):
    outbound.drain_notices(store=store, bus=bus, deliver=_deliver(api))


# --- delivery when bound --------------------------------------------------------------------


def test_drain_delivers_fifo_and_stamps(store, bus, xdg_tmp) -> None:
    _bind(store)
    store.queue_notice("first")
    store.queue_notice("second")
    with FakeTelegramAPI() as api:
        _drain(store, bus, api)
        assert [m["text"] for m in api.outbox] == ["first", "second"]
    assert store.count_queued_notices() == 0  # both stamped delivered
    delivered = [e for e in bus.store.read() if e.kind == "message.delivered"]
    assert len(delivered) == 2


def test_drain_transport_failure_bumps_attempts_and_raises(store, bus, xdg_tmp) -> None:
    _bind(store)
    nid = store.queue_notice("ping")

    def failing_deliver(chat_id, text, controls=None):
        raise ChannelError("Bot API sendMessage HTTP 500")

    with pytest.raises(ChannelError):
        outbound.drain_notices(store=store, bus=bus, deliver=failing_deliver)
    queued = store.list_queued_notices()
    assert len(queued) == 1 and queued[0]["id"] == nid and queued[0]["attempts"] == 1


# --- no-op when unbound / paused ------------------------------------------------------------


def test_drain_is_noop_when_unbound(store, bus, xdg_tmp) -> None:
    store.queue_notice("later")  # no bind: catchup is the delivery path
    with FakeTelegramAPI() as api:
        _drain(store, bus, api)
        assert api.outbox == []
    assert store.count_queued_notices() == 1


def test_drain_is_noop_when_paused(store, bus, xdg_tmp) -> None:
    _bind(store)
    store.set_paused(True, source="test")
    store.queue_notice("held")
    with FakeTelegramAPI() as api:
        _drain(store, bus, api)
        assert api.outbox == []
    assert store.count_queued_notices() == 1


# --- escalation push ------------------------------------------------------------------------


def test_escalation_open_queues_one_notice(make_ctx, store, bus) -> None:
    bus.subscribe(outbound.escalation_notifier(store))
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:t1",
        side="sell",
        market="carousell",
        counterpart_handle="b",
        item_id=item["id"],
    )
    ctx = make_ctx(TIER_ATTENDED)
    dispatch("escalate", {"thread_id": "carousell:t1", "open_question": "Accept $70?"}, ctx)
    dispatch(
        "escalate", {"thread_id": "carousell:t1", "open_question": "still?"}, ctx
    )  # idempotent
    queued = store.list_queued_notices()
    assert len(queued) == 1  # one escalation -> one notice, no duplicate
    assert "Accept $70?" in queued[0]["text"] and queued[0]["ref"] == "carousell:t1"


def _escalating_thread(store, tid="carousell:t2"):
    item = store.create_item(title="Fan", list_price=180.0, currency="SGD")
    store.create_thread(
        thread_id=tid, side="sell", market="carousell", counterpart_handle="b", item_id=item["id"]
    )


def test_escalation_push_carries_its_options_as_buttons(make_ctx, store, bus) -> None:
    bus.subscribe(outbound.escalation_notifier(store))
    _escalating_thread(store)
    dispatch(
        "escalate",
        {
            "thread_id": "carousell:t2",
            "open_question": "meet at Orchard, or checkout?",
            "options": ["🔗 Send checkout link", "🤝 I'll handle it"],
        },
        make_ctx(TIER_ATTENDED),
    )

    notice = store.list_queued_notices()[0]
    assert notice["controls"] == [
        ["🔗 Send checkout link", f"n{notice['id']}:a0"],
        ["🤝 I'll handle it", f"n{notice['id']}:a1"],
    ]


def test_an_escalation_without_options_still_pushes(make_ctx, store, bus) -> None:
    """A question only the seller can answer has no fixed answers — the ask still has to arrive."""
    bus.subscribe(outbound.escalation_notifier(store))
    _escalating_thread(store)
    dispatch(
        "escalate",
        {"thread_id": "carousell:t2", "open_question": "how should I answer this?"},
        make_ctx(TIER_ATTENDED),
    )

    notice = store.list_queued_notices()[0]
    assert "how should I answer this?" in notice["text"]
    assert notice["controls"] is None


def test_delivery_hands_an_asks_buttons_to_the_provider(store, bus, xdg_tmp) -> None:
    """The drain reads controls off the durable row, so an ask's keyboard survives a restart."""
    _bind(store)
    store.queue_notice("How do you want to close?", options=["Checkout", "Myself"])
    seen: list = []

    outbound.drain_notices(
        store=store, bus=bus, deliver=lambda chat_id, text, controls=None: seen.append(controls)
    )

    assert seen == [[["Checkout", "n1:a0"], ["Myself", "n1:a1"]]]


# --- a buyer nobody answered ---------------------------------------------------------------------
#
# The gap this closes: on 2026-08-29 two buyers waited from 03:14 with no reply and no word to the
# seller, while the reply lane respawned a pass every 28 seconds. Every pass was ledgered class=ok,
# so nothing anywhere said a buyer was waiting. Silence has to be reportable.


def _waiting(store, tid="carousell:1", *, handle="bob", title="Teak lamp", ts=10.0):
    item = store.create_item(title=title, list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id=tid,
        side="sell",
        market="carousell",
        counterpart_handle=handle,
        item_id=item["id"],
    )
    store.record_inbound(tid, msg_id="m1", text="still available?", ts=ts)
    return item


def _texts(store):
    return [n["text"] for n in store.list_queued_notices()]


def test_a_long_unanswered_buyer_is_reported_once(store, bus, xdg_tmp) -> None:
    _bind(store)
    _waiting(store, ts=0.0)
    late = outbound.BUYER_WAITING_AGE_SEC + 1

    outbound.buyer_waiting_notice(store=store, now=late)
    outbound.buyer_waiting_notice(store=store, now=late + 60)  # tick repeats, notice does not
    assert len(_texts(store)) == 1
    assert "bob" in _texts(store)[0] and "Teak lamp" in _texts(store)[0]


def test_a_buyer_inside_the_window_is_not_reported_yet(store, bus, xdg_tmp) -> None:
    """A reply in flight is normal. Only silence past the threshold is news."""
    _bind(store)
    _waiting(store, ts=0.0)
    outbound.buyer_waiting_notice(store=store, now=outbound.BUYER_WAITING_AGE_SEC - 1)
    assert _texts(store) == []


def test_the_notice_is_not_holdable_so_quiet_hours_cannot_swallow_it(store, bus, xdg_tmp) -> None:
    """A holdable notice about a buyer waiting overnight would arrive exactly when it stopped
    mattering. A waiting buyer is the seller's call, not a routine update."""
    _bind(store)
    _waiting(store, ts=0.0)
    outbound.buyer_waiting_notice(store=store, now=outbound.BUYER_WAITING_AGE_SEC + 1)
    assert store.list_queued_notices()[0]["holdable"] is False


def test_answering_the_buyer_stops_the_report(store, bus, xdg_tmp) -> None:
    _bind(store)
    _waiting(store, ts=0.0)
    store.record_manual_reply("carousell:1", "yes, still available")
    outbound.buyer_waiting_notice(store=store, now=outbound.BUYER_WAITING_AGE_SEC + 1)
    assert _texts(store) == []


def test_a_second_question_after_an_answer_reports_again(store, bus, xdg_tmp) -> None:
    """The guard is the message waited on, not the thread — so a buyer who is answered and then
    ignored again is not silently written off by the first notice's ref."""
    _bind(store)
    _waiting(store, ts=0.0)
    late = outbound.BUYER_WAITING_AGE_SEC + 1
    outbound.buyer_waiting_notice(store=store, now=late)
    assert len(_texts(store)) == 1

    store.record_inbound("carousell:1", msg_id="m2", text="hello?", ts=late)
    outbound.buyer_waiting_notice(store=store, now=late * 2 + 1)
    assert len(_texts(store)) == 2


def test_an_escalated_thread_is_not_double_reported(store, bus, xdg_tmp) -> None:
    """An open escalation already put the decision in front of the seller."""
    _bind(store)
    _waiting(store, ts=0.0)
    store.escalate("carousell:1", open_question="Buyer offered $30 — take it?")
    outbound.buyer_waiting_notice(store=store, now=outbound.BUYER_WAITING_AGE_SEC + 1)
    assert _texts(store) == []


def test_a_paused_agent_reports_nothing(store, bus, xdg_tmp) -> None:
    _bind(store)
    _waiting(store, ts=0.0)
    store.set_paused(True, source="test")
    outbound.buyer_waiting_notice(store=store, now=outbound.BUYER_WAITING_AGE_SEC + 1)
    assert _texts(store) == []


def test_an_escalation_names_the_conversation_it_is_about(make_ctx, store, bus) -> None:
    """A question that forgets which chat it is about produces a notice nobody can act on. The
    thread carries the marketplace, the buyer and the item, so none of the three depends on the
    model remembering — and a seller with two marketplaces open needs all three to find it."""
    bus.subscribe(outbound.escalation_notifier(store))
    item = store.create_item(title="IKEA Elloven monitor stand", list_price=15.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:t9",
        side="sell",
        market="carousell",
        counterpart_handle="emline",
        item_id=item["id"],
    )

    dispatch(
        "escalate",
        {
            "thread_id": "carousell:t9",
            "open_question": "No floor is set for this item — $10 offer?",
        },
        make_ctx(TIER_ATTENDED),
    )

    text = _texts(store)[0]
    assert text.startswith("Needs your call — Carousell · emline · IKEA Elloven monitor stand:")


def test_only_the_part_a_question_already_names_is_dropped(make_ctx, store, bus) -> None:
    """Per field, not all-or-nothing. The old rule threw the whole reference away because the
    title appeared, which is how an ask naming the buyer and the item still never said which
    marketplace it was on."""
    bus.subscribe(outbound.escalation_notifier(store))
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:t8",
        side="sell",
        market="carousell",
        counterpart_handle="bex",
        item_id=item["id"],
    )

    dispatch(
        "escalate",
        {"thread_id": "carousell:t8", "open_question": "bex offered $70 for the Teak lamp?"},
        make_ctx(TIER_ATTENDED),
    )

    assert _texts(store)[0] == "Needs your call — Carousell: bex offered $70 for the Teak lamp?"


def test_a_short_handle_the_question_does_not_name_survives(make_ctx, store, bus) -> None:
    """The suppression matches whole runs. A one-letter handle is inside almost any sentence, and
    a bare substring test would drop the buyer from exactly the asks that never name them."""
    bus.subscribe(outbound.escalation_notifier(store))
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:t7",
        side="sell",
        market="carousell",
        counterpart_handle="b",
        item_id=item["id"],
    )

    dispatch(
        "escalate",
        {"thread_id": "carousell:t7", "open_question": "Accept $70 for the Teak lamp?"},
        make_ctx(TIER_ATTENDED),
    )

    assert _texts(store)[0] == "Needs your call — Carousell · b: Accept $70 for the Teak lamp?"
