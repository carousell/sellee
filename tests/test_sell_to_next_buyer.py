"""sell_to_next_buyer: the seller's "🔗 Send them the link" tap on a withdrawal notice, closed in
one tool — re-check that buyer, reserve the item for them, mint the link at their offer without
touching the floor, and send it to them."""

from __future__ import annotations

import pytest

from sellee.config import Config
from sellee.rail.client import RailToolError
from sellee.store import Store
from sellee.tools import negotiate as negotiate_tools
from sellee.tools.registry import ToolError, UnknownTool, dispatch

CFG = Config()
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
_URL = "https://www.carousell.ai/checkout/c9e22772"
_FLOOR = 90.0


class FakeRail:
    def __init__(self, *, error=None):
        self.minted: list = []
        self._error = error

    def create_checkout(self, args):
        if self._error is not None:
            raise self._error
        self.minted.append(args)
        return {"checkout_url": _URL}


class Sink:
    def __init__(self):
        self.sends: list = []

    def send(self, thread, text, kind, intent_id):
        self.sends.append((thread["thread_id"], text))


def _item(store: Store, *, list_price=100.0):
    item = store.create_item(title="Navy scarf", list_price=list_price, currency="USD")
    store.set_floor(item["id"], _FLOOR, "seller")
    store.record_listing_url(item["id"], "carousell-ai", "https://www.carousell.ai/listing/L1")
    return item


def _thread(store: Store, item: dict, tid: str, handle: str | None = None) -> None:
    store.create_thread(
        thread_id=tid,
        side="sell",
        market="fb",
        counterpart_handle=handle or tid.split(":")[1],
        item_id=item["id"],
    )


def _offer(store: Store, item: dict, tid: str, offer) -> dict:
    return store.negotiate_offer(item["id"], tid, "", offer, config=CFG)


def _withdraw(store: Store, item: dict, tid: str) -> int | None:
    store.record_inbound(tid, msg_id=f"{tid}-out", text="nvm", ts=500.0)
    result = store.negotiate_withdraw(
        item["id"],
        tid,
        msg_id=f"{tid}-out",
        notice=lambda r: negotiate_tools.withdrawal_notice("", "found another", r),
    )
    return result["notice_id"]


def _ctx(make_ctx, rail=None, sink=None, tier="pass:channel"):
    return make_ctx(
        tier,
        rail_factory=(lambda: rail) if rail is not None else None,
        reply_sink=sink,
        config=_FAST,
    )


def _holder_backs_out(store: Store) -> tuple:
    """b offered 80, under the floor; a took it at list and then backed out. The notice names b."""
    item = _item(store)
    for tid in ("fb:a", "fb:b"):
        _thread(store, item, tid)
    _offer(store, item, "fb:b", 80)
    assert _offer(store, item, "fb:a", 100)["decision"] == "accept_fcfs"
    return item, _withdraw(store, item, "fb:a")


def _sell(make_ctx, notice_id, rail, sink, tier="pass:channel") -> dict:
    return dispatch(
        "sell_to_next_buyer", {"notice_id": notice_id}, _ctx(make_ctx, rail, sink, tier)
    )


# --- the tap --------------------------------------------------------------------------------------


def test_the_tap_reserves_mints_and_sends_at_the_named_offer(make_ctx, store) -> None:
    item, notice_id = _holder_backs_out(store)
    rail, sink = FakeRail(), Sink()

    result = _sell(make_ctx, notice_id, rail, sink)

    assert result["status"] == "sent" and result["delivered"] == "yes"
    assert result["buyer"] == "b" and result["price"] == "80 USD"
    status = store.negotiate_status(item["id"])
    assert status["item_state"] == "reserved_provisional"
    assert status["front_runner"] == {"thread_id": "fb:b", "amount": 80, "kind": "fcfs"}
    assert rail.minted == [{"listing_id": "L1", "agreed_price_cents": 8000}]
    ((to, text),) = sink.sends
    assert to == "fb:b" and _URL in text and "80 USD" in text


def test_the_tap_sells_below_the_floor_without_lowering_it(make_ctx, store) -> None:
    """The seller's tap authorises this buyer at this price; it is not a new floor for everyone."""
    item, notice_id = _holder_backs_out(store)

    _sell(make_ctx, notice_id, FakeRail(), Sink())

    assert store.get_floor(item["id"])["floor"] == _FLOOR
    with pytest.raises(ToolError, match="not acceptable"):
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": item["id"], "thread_id": "fb:c", "agreed_price": 80},
            _ctx(make_ctx, FakeRail()),
        )


def test_the_reservation_turns_away_the_next_buyer_to_accept(make_ctx, store) -> None:
    item, notice_id = _holder_backs_out(store)
    _thread(store, item, "fb:c")

    _sell(make_ctx, notice_id, FakeRail(), Sink())

    assert _offer(store, item, "fb:c", 100)["decision"] == "fcfs_taken"


def test_in_bidding_the_tap_confirms_the_next_bid(make_ctx, store) -> None:
    item = _item(store)
    for tid in ("fb:b", "fb:c"):
        _thread(store, item, tid)
    _offer(store, item, "fb:b", 120)
    _offer(store, item, "fb:c", 130)
    notice_id = _withdraw(store, item, "fb:c")

    result = _sell(make_ctx, notice_id, FakeRail(), Sink())

    assert result["price"] == "120 USD"
    status = store.negotiate_status(item["id"])
    assert status["item_state"] == "reserved_provisional"
    assert status["front_runner"] == {"thread_id": "fb:b", "amount": 120, "kind": "bid"}
    assert status["buyers"]["fb:b"]["status"] == "won"


def test_the_link_goes_to_the_buyer_the_notice_meant_not_a_namesake(make_ctx, store) -> None:
    """Display names are not unique; the ask keeps the thread it meant."""
    item = _item(store)
    _thread(store, item, "fb:a")
    _thread(store, item, "fb:bob1", handle="Bob")
    _thread(store, item, "fb:bob2", handle="Bob")
    _offer(store, item, "fb:bob1", 70)
    _offer(store, item, "fb:bob2", 85)
    _offer(store, item, "fb:a", 100)
    notice_id = _withdraw(store, item, "fb:a")
    sink = Sink()

    _sell(make_ctx, notice_id, FakeRail(), sink)

    assert [to for to, _text in sink.sends] == ["fb:bob2"]


def test_a_second_call_does_not_send_the_link_again(make_ctx, store) -> None:
    _item_, notice_id = _holder_backs_out(store)
    rail, sink = FakeRail(), Sink()
    _sell(make_ctx, notice_id, rail, sink)

    again = _sell(make_ctx, notice_id, rail, sink)

    assert again["status"] == "already_sent"
    assert len(sink.sends) == 1 and len(rail.minted) == 1


def test_the_seller_tier_runs_it_and_the_reply_pass_cannot(make_ctx, store) -> None:
    _item_, notice_id = _holder_backs_out(store)

    assert _sell(make_ctx, notice_id, FakeRail(), Sink(), tier="attended")["status"] == "sent"
    with pytest.raises(UnknownTool):
        _sell(make_ctx, notice_id, FakeRail(), Sink(), tier="pass:reply")


# --- a stale tap changes nothing ------------------------------------------------------------------


def _next_buyer_also_left(store, item) -> None:
    store.negotiate_withdraw(
        item["id"], "fb:b", msg_id="b-out", notice=lambda r: ("backed out", None)
    )


def _item_sold(store, item) -> None:
    _thread(store, item, "fb:c")
    store.negotiate_confirm_sold(item["id"], "fb:c")


def _someone_else_took_it(store, item) -> None:
    _thread(store, item, "fb:c")
    assert _offer(store, item, "fb:c", 100)["decision"] == "accept_fcfs"


def _offer_dropped(store, item) -> None:
    _next_buyer_also_left(store, item)
    _offer(store, item, "fb:b", 60)  # back in, at less than the notice named


@pytest.mark.parametrize(
    ("since", "says"),
    [
        (_next_buyer_also_left, "no longer"),
        (_item_sold, "sold"),
        (_someone_else_took_it, "another buyer"),
        (_offer_dropped, "below"),
    ],
)
def test_a_stale_tap_reserves_mints_and_sends_nothing(make_ctx, store, since, says) -> None:
    item, notice_id = _holder_backs_out(store)
    since(store, item)
    before = store.negotiate_status(item["id"])
    rail, sink = FakeRail(), Sink()

    with pytest.raises(ToolError, match=says):
        _sell(make_ctx, notice_id, rail, sink)

    assert store.negotiate_status(item["id"]) == before
    assert rail.minted == [] and sink.sends == []


def test_a_notice_that_named_no_one_sells_to_no_one(make_ctx, store) -> None:
    item = _item(store)
    _thread(store, item, "fb:a")
    _offer(store, item, "fb:a", 100)
    notice_id = _withdraw(store, item, "fb:a")  # nobody else offered

    with pytest.raises(ToolError, match="no buyer"):
        _sell(make_ctx, notice_id, FakeRail(), Sink())


def test_paused_reserves_nothing(make_ctx, store) -> None:
    item, notice_id = _holder_backs_out(store)
    store.set_paused(True)
    before = store.negotiate_status(item["id"])

    with pytest.raises(ToolError, match="paused"):
        _sell(make_ctx, notice_id, FakeRail(), Sink())

    assert store.negotiate_status(item["id"]) == before


# --- when the link cannot be minted or sent -------------------------------------------------------


def test_a_failed_mint_releases_the_reservation(make_ctx, store) -> None:
    item, notice_id = _holder_backs_out(store)
    before = store.negotiate_status(item["id"])
    sink = Sink()

    with pytest.raises(ToolError, match="not held for them"):
        _sell(make_ctx, notice_id, FakeRail(error=RailToolError("rail is down")), sink)

    assert store.negotiate_status(item["id"]) == before
    assert sink.sends == []


def test_a_link_that_could_not_be_sent_holds_nothing_and_goes_out_later(make_ctx, store) -> None:
    """Holding the item for a buyer who never got the link would keep it from everyone else."""
    item, notice_id = _holder_backs_out(store)
    before = store.negotiate_status(item["id"])
    rail = FakeRail()

    first = dispatch(
        "sell_to_next_buyer", {"notice_id": notice_id}, _ctx(make_ctx, rail, sink=None)
    )

    assert first["delivered"] == "no"
    assert store.negotiate_status(item["id"]) == before
    sink = Sink()
    second = _sell(make_ctx, notice_id, rail, sink)
    assert second["status"] == "sent" and len(sink.sends) == 1
    assert store.negotiate_status(item["id"])["front_runner"]["thread_id"] == "fb:b"
    assert len(rail.minted) == 1  # the link minted the first time is the one sent


def test_another_send_in_flight_is_not_taken_for_ours(make_ctx, store) -> None:
    """`in_flight` is some other message being typed on that thread; ours never left."""
    from sellee.tools import reply

    item, notice_id = _holder_backs_out(store)
    before = store.negotiate_status(item["id"])
    sink = Sink()
    reply._SENDING.add("fb:b")
    try:
        first = _sell(make_ctx, notice_id, FakeRail(), sink)
    finally:
        reply._SENDING.discard("fb:b")

    assert first["status"] == "in_flight" and sink.sends == []
    assert store.negotiate_status(item["id"]) == before
    assert _sell(make_ctx, notice_id, FakeRail(), sink)["status"] == "sent"


def test_a_send_that_may_have_landed_is_never_repeated(make_ctx, store) -> None:
    class UnverifiedSink:
        def __init__(self):
            self.sends = 0

        def send(self, thread, text, kind, intent_id):
            self.sends += 1
            store.mark_intent_sent_unverified(intent_id)
            raise RuntimeError("could not read the message back")

    item, notice_id = _holder_backs_out(store)
    sink = UnverifiedSink()

    first = _sell(make_ctx, notice_id, FakeRail(), sink)
    again = _sell(make_ctx, notice_id, FakeRail(), sink)

    assert first["status"] == "send_unverified" and again["status"] == "already_sent"
    assert sink.sends == 1
    assert store.negotiate_status(item["id"])["front_runner"]["thread_id"] == "fb:b"


def test_a_second_link_out_is_flagged(make_ctx, store) -> None:
    item, notice_id = _holder_backs_out(store)
    store.record_checkout(
        sale_id="s-a",
        item_id=item["id"],
        thread_id="fb:a",
        checkout_url=_URL + "-a",
        price=100,
        currency="USD",
    )

    assert _sell(make_ctx, notice_id, FakeRail(), Sink())["another_link_out"] is True


# --- the notice remembers who it meant ------------------------------------------------------------


def test_the_ask_is_recorded_only_with_a_next_buyer(store) -> None:
    item, notice_id = _holder_backs_out(store)
    _thread(store, item, "fb:z")
    _offer(store, item, "fb:z", 70)
    lone = _withdraw(store, item, "fb:z")  # held nothing, so names no one

    assert store.next_buyer_ask(notice_id)["thread_id"] == "fb:b"
    assert store.next_buyer_ask(lone) is None
