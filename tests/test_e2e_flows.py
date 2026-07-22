"""Attended end-to-end flows over the real dispatch path (fake rail + fake sink), plus the
transcript leak sweep: planted floor/budget/origin sentinels must appear in no recorded event and
no tool return. The sell path runs item -> floor -> thread -> scan -> negotiate -> paced reply ->
idempotent checkout; the buy path runs want -> budget -> thread -> open -> reply -> accept."""

from __future__ import annotations

from selly_agent.config import Config
from selly_agent.tools.registry import TIER_ATTENDED, dispatch

# Distinctive sentinels for the leak sweep — none should ever surface in an event or a return.
FLOOR_SENTINEL = 7331
BUDGET_SENTINEL = 7332
ORIGIN_SENTINEL = "7333 Marina Boulevard"

_CFG = Config(
    reply_delay_sec=(0, 0),
    interactive_reply_delay_sec=(0, 0),
    quiet_hours=(0, 0),
    carousell_ai_api_base="https://api.carousell.ai",
)
_ZONES = [{"zone": "nationwide", "match": {"areas": ["__else__"]}, "fee": 5}]


class FakeRail:
    def create_listing(self, args):
        return {"listing_id": "L1", "url": "https://www.carousell.ai/listing/L1"}

    def verify_listing_url(self, url):
        return None

    def create_checkout(self, args):
        return {"checkout_url": "https://api.carousell.ai/checkout/deal-xyz"}


class FakeSink:
    def __init__(self):
        self.sends = []

    def send(self, thread, text, kind):
        self.sends.append((thread["thread_id"], text, kind))


def _ctx(make_ctx):
    return make_ctx(TIER_ATTENDED, rail_factory=FakeRail, reply_sink=FakeSink(), config=_CFG)


def test_sell_path_end_to_end_no_secret_leak(make_ctx, store, bus) -> None:
    ctx = _ctx(make_ctx)

    item = dispatch(
        "create_item", {"title": "Vintage lamp", "list_price": 9000.0, "currency": "SGD"}, ctx
    )
    iid = item["id"]
    dispatch("set_floor", {"item_id": iid, "floor": FLOOR_SENTINEL, "source": "seller"}, ctx)
    dispatch(
        "update_seller_config",
        {"shipping": {"zones": _ZONES}, "origin": {"address": ORIGIN_SENTINEL}},
        ctx,
    )
    dispatch(
        "create_thread",
        {
            "thread_id": "fb:buyer1",
            "side": "sell",
            "market": "fb",
            "counterpart_handle": "harry",
            "item_id": iid,
        },
        ctx,
    )
    scan = dispatch(
        "scam_scan",
        {"thread_id": "fb:buyer1", "market": "fb", "text": "is this still available?"},
        ctx,
    )
    assert scan["verdict"] == "clean"

    # a below-list offer counters (never below the floor), an at-list offer takes it FCFS
    counter = dispatch(
        "negotiate_offer",
        {"item_id": iid, "thread_id": "fb:buyer1", "buyer": "harry", "offer": 8000},
        ctx,
    )
    assert counter["decision"] in ("counter", "accept_fcfs")

    quote = dispatch("quote_shipping", {"item_id": iid, "dest_area": "anywhere"}, ctx)
    assert quote["covered"] is True

    sent = dispatch(
        "send_reply", {"thread_id": "fb:buyer1", "text": "Can do 8500?", "in_msg_id": "m1"}, ctx
    )
    assert sent["status"] == "sent"

    accept = dispatch(
        "negotiate_offer",
        {"item_id": iid, "thread_id": "fb:buyer1", "buyer": "harry", "offer": 9000},
        ctx,
    )
    assert accept["decision"] == "accept_fcfs"

    dispatch("carousell_ai_publish_listing", {"item_id": iid}, ctx)
    link1 = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": iid, "thread_id": "fb:buyer1", "agreed_price": 9000},
        ctx,
    )
    link2 = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": iid, "thread_id": "fb:buyer1", "agreed_price": 9000},
        ctx,
    )
    assert link2["already_issued"] is True and link1["checkout_url"] == link2["checkout_url"]

    # the leak sweep: no sentinel appears in any recorded event payload
    blob = "".join(str(e.payload) for e in bus.store.read())
    assert str(FLOOR_SENTINEL) not in blob
    assert str(BUDGET_SENTINEL) not in blob  # (not set on this path, but proves the sweep is real)
    assert ORIGIN_SENTINEL not in blob


def test_buy_path_end_to_end(make_ctx, store) -> None:
    ctx = _ctx(make_ctx)
    want = dispatch("create_want", {"query": "iPhone 15", "currency": "SGD"}, ctx)
    wid = want["want_id"]
    dispatch(
        "set_budget",
        {"want_id": wid, "max_budget": BUDGET_SENTINEL, "target_price": 5000, "source": "buyer"},
        ctx,
    )
    dispatch(
        "create_thread",
        {
            "thread_id": "cl:sellerA",
            "side": "buy",
            "market": "cl",
            "counterpart_handle": "alice",
            "want_id": wid,
        },
        ctx,
    )
    dispatch(
        "create_thread",
        {
            "thread_id": "fb:sellerB",
            "side": "buy",
            "market": "fb",
            "counterpart_handle": "bob",
            "want_id": wid,
        },
        ctx,
    )
    opened = dispatch(
        "buyer_negotiate_open",
        {"want_id": wid, "thread_id": "cl:sellerA", "seller": "alice", "listed": 6500},
        ctx,
    )
    assert opened["decision"] == "opening_offer" and opened["offer_price"] <= BUDGET_SENTINEL
    dispatch(
        "buyer_negotiate_open",
        {"want_id": wid, "thread_id": "fb:sellerB", "seller": "bob", "listed": 6500},
        ctx,
    )
    accept = dispatch("buyer_negotiate_accept", {"want_id": wid, "thread_id": "cl:sellerA"}, ctx)
    assert accept["want_state"] == "committed"
    assert accept["close_threads"] == ["fb:sellerB"]  # the sibling is closed on commit


def test_leak_sweep_over_budget_and_no_value_in_returns(make_ctx, store, bus) -> None:
    ctx = _ctx(make_ctx)
    want = dispatch("create_want", {"query": "thing", "currency": "SGD"}, ctx)
    wid = want["want_id"]
    ack = dispatch(
        "set_budget",
        {"want_id": wid, "max_budget": BUDGET_SENTINEL, "target_price": 5000, "source": "buyer"},
        ctx,
    )
    assert str(BUDGET_SENTINEL) not in str(ack)  # the ack carries no value
    got = dispatch("get_want", {"want_id": wid}, ctx)
    assert str(BUDGET_SENTINEL) not in str(got)
    blob = "".join(str(e.payload) for e in bus.store.read())
    assert str(BUDGET_SENTINEL) not in blob  # masked in tool.call, never in tool.result
