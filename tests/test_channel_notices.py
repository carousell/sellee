"""Notices: the drain lane delivers to a bound chat FIFO and stamps them; unbound or paused it is
a no-op (catchup delivers instead); a transport failure bumps attempts and re-raises so the
scheduler backs off. The escalation-push subscriber queues exactly one notice per escalation.
"""

from __future__ import annotations

import pytest

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from selly_agent import secrets
from selly_agent.channel import outbound
from selly_agent.channel.telegram.transport import ChannelError, TelegramClient
from selly_agent.tools import TIER_ATTENDED
from selly_agent.tools.registry import dispatch


def _bind(store):
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1)


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
