"""The two ledgers activity updates are built from: every offer the engine decided, and every
thing done on a seller's listing they have not been told yet. Both are written inside the
transaction that produced the fact, and neither may ever carry the confidential floor."""

from __future__ import annotations

import pytest
from tests.conftest import leak_paths

from sellee.config import Config
from sellee.engines import pacing
from sellee.store import Scope, Store
from sellee.store import updates as updates_store
from sellee.store.updates import BuyerAnsweredFacts, OfferDeclinedFacts, TellRecord
from sellee.tools.registry import dispatch

CFG = Config()
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
LIST_PRICE = 100.0
# Distinctive on purpose: no counter the engine walks from a list price of 100 in steps of 10 can
# land on it, so finding it anywhere in a ledger means the floor itself got there.
FLOOR = 73.0


def _item(store: Store, *, title="Trek Emonda 54cm", floor=FLOOR):
    item = store.create_item(title=title, list_price=LIST_PRICE, currency="SGD")
    if floor is not None:
        store.set_floor(item["id"], floor, "seller")
    return item


def _offer(store: Store, item_id: str, thread_id: str, amount: float, handle="dana"):
    return store.negotiate_offer(item_id, thread_id, handle, amount, config=CFG)


def _tell_rows(store: Store) -> list[dict]:
    return [dict(r) for r in store._db.query("SELECT * FROM tells ORDER BY id")]


# --- offer_declined tells ----------------------------------------------------------------------


@pytest.mark.parametrize(("amount", "asks"), [(FLOOR - 1, 1), (FLOOR, 0), (FLOOR + 1, 0)])
def test_below_floor_is_judged_against_the_floor_the_engine_was_handed(
    store: Store, amount, asks
) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:1", amount)
    assert len(store.untold_tells()) == asks


def test_the_same_offer_handed_over_again_writes_nothing(store: Store) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:1", 65)
    again = _offer(store, item["id"], "fb:1", 65)

    assert again["decision"] == "repeat_offer"
    assert len(_tell_rows(store)) == 1


def test_an_offer_held_for_a_floor_not_yet_set_writes_nothing(store: Store) -> None:
    item = _item(store, floor=None)
    res = _offer(store, item["id"], "fb:1", 65)
    assert res["decision"] == "needs_floor"
    assert _tell_rows(store) == []


@pytest.mark.parametrize("amount", [65, 30])
def test_an_offer_under_the_floor_leaves_a_decision_for_the_seller(store: Store, amount) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:1", amount, handle="dana")

    tells = store.untold_tells()
    assert len(tells) == 1
    tell = tells[0]
    assert (tell["kind"], tell["item_id"], tell["thread_id"]) == (
        "offer_declined",
        item["id"],
        "fb:1",
    )
    assert tell["decision"] is True
    assert tell["facts"] == {
        "buyer_handle": "dana",
        "amount": amount,
        "currency": "SGD",
        "item_title": "Trek Emonda 54cm",
    }


@pytest.mark.parametrize("amount", [FLOOR, 85, 100, 120])
def test_an_offer_at_or_over_the_floor_tells_the_seller_nothing(store: Store, amount) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:1", amount)
    assert store.untold_tells() == []


def test_a_buyer_held_firm_under_the_floor_leaves_a_decision_too(store: Store) -> None:
    """Out of counters, the buyer is held at the last price: still their number turned down."""
    item = _item(store)
    for amount in (60, 61, 62, 63):
        res = _offer(store, item["id"], "fb:1", amount)

    assert res["decision"] == "hold_firm"
    held = [t for t in store.untold_tells() if t["facts"]["amount"] == 63]
    assert len(held) == 1 and held[0]["decision"] is True


def test_a_buyer_walking_up_under_the_floor_is_one_question_for_the_seller(store: Store) -> None:
    """The seller is asked once, about the newest offer; the rounds it replaced are retired untold,
    not carried by any notice."""
    item = _item(store)
    for amount in (40, 55, 60, 65, 70, 72):
        _offer(store, item["id"], "fb:walk", amount, handle="dana")

    (tell,) = store.untold_tells()
    assert (tell["thread_id"], tell["facts"]["amount"]) == ("fb:walk", 72)
    retired = [r for r in _tell_rows(store) if r["id"] != tell["id"]]
    assert len(retired) == 5
    assert all(r["told_ts"] is not None and r["notice_id"] is None for r in retired)


def test_a_newer_offer_replaces_only_that_buyers_untold_question(store: Store) -> None:
    """Another buyer's question, or one on another item, still stands; so does one the seller was
    already told, which keeps the notice that carried it."""
    item, other = _item(store), _item(store, title="lamp")
    _offer(store, item["id"], "fb:1", 40, handle="dana")
    (told,) = store.untold_tells()
    store.mark_tells_told([told["id"]], notice_id=9)
    _offer(store, item["id"], "fb:2", 45, handle="eli")
    _offer(store, other["id"], "fb:1", 30, handle="dana")
    _offer(store, item["id"], "fb:1", 50, handle="dana")

    untold = [(t["item_id"], t["thread_id"], t["facts"]["amount"]) for t in store.untold_tells()]
    assert untold == [(item["id"], "fb:2", 45), (other["id"], "fb:1", 30), (item["id"], "fb:1", 50)]
    assert {r["id"]: r["notice_id"] for r in _tell_rows(store)}[told["id"]] == 9


def _retired_untold(store: Store, tell_id: int) -> bool:
    row = {r["id"]: r for r in _tell_rows(store)}[tell_id]
    return row["told_ts"] is not None and row["notice_id"] is None


@pytest.mark.parametrize(
    ("first", "then", "decision"),
    [
        (72, 74, "counter"),
        (72, 80, "accept_fcfs"),
        (60, LIST_PRICE, "accept_fcfs"),
        (60, LIST_PRICE + 20, "bid_lead"),
    ],
)
def test_a_buyer_whose_next_offer_clears_the_floor_is_no_question_for_the_seller(
    store: Store, first, then, decision
) -> None:
    """Their offer under the floor is no longer standing, so the seller is not asked to lower the
    minimum for a buyer already countered at it or already agreed."""
    item = _item(store)
    _offer(store, item["id"], "fb:1", first, handle="dana")
    (asked,) = store.untold_tells()

    res = _offer(store, item["id"], "fb:1", then, handle="dana")

    assert res["decision"] == decision
    assert store.untold_tells() == []
    assert _retired_untold(store, asked["id"])


def test_a_decision_the_engine_adds_later_asks_the_seller_by_default(store: Store) -> None:
    """Only the decisions known to leave nothing for a lower minimum to win are kept quiet."""
    item = _item(store)
    with store._db.transaction() as conn:
        updates_store.record_offer_decision_in_txn(
            conn,
            item_id=item["id"],
            thread_id="fb:1",
            handle="dana",
            offer=50,
            currency="SGD",
            result={"decision": "a_decision_not_yet_written"},
            floor=FLOOR,
        )

    (tell,) = store.untold_tells()
    assert (tell["kind"], tell["decision"], tell["facts"]["amount"]) == ("offer_declined", True, 50)


def _sold(store: Store, item_id: str) -> None:
    store.negotiate_confirm_sold(item_id, "fb:winner")


def _held_by_a_rival(store: Store, item_id: str) -> None:
    _offer(store, item_id, "fb:rival", LIST_PRICE, handle="rival")


def _in_an_auction(store: Store, item_id: str) -> None:
    _offer(store, item_id, "fb:rival", LIST_PRICE + 20, handle="rival")


@pytest.mark.parametrize(
    ("setup", "decision"),
    [(_sold, "sold"), (_held_by_a_rival, "fcfs_taken"), (_in_an_auction, "bid_outbid")],
)
def test_an_offer_under_the_floor_not_weighed_on_price_asks_the_seller_nothing(
    store: Store, setup, decision
) -> None:
    """A lower minimum cannot win back a buyer told the item is sold, held for someone else, or
    out-bid, so the seller is not asked about their offer."""
    item = _item(store)
    setup(store, item["id"])
    res = _offer(store, item["id"], "fb:1", 50, handle="dana")

    assert res["decision"] == decision
    assert [t for t in store.untold_tells() if t["kind"] == "offer_declined"] == []


@pytest.mark.parametrize(
    ("setup", "decision"),
    [(_sold, "sold"), (_held_by_a_rival, "fcfs_taken"), (_in_an_auction, "bid_outbid")],
)
def test_a_buyer_whose_next_offer_is_turned_away_for_another_reason_is_no_question_either(
    store: Store, setup, decision
) -> None:
    """Once the item is sold, held for someone else or out-bid, a lower minimum cannot win this
    buyer back, so the question they left before it is retired with their next offer."""
    item = _item(store)
    _offer(store, item["id"], "fb:1", 60, handle="dana")
    (asked,) = store.untold_tells()
    setup(store, item["id"])

    res = _offer(store, item["id"], "fb:1", 65, handle="dana")

    assert res["decision"] == decision
    assert [t for t in store.untold_tells() if t["kind"] == "offer_declined"] == []
    assert _retired_untold(store, asked["id"])


def _without_row_ids(rows) -> list[dict]:
    """A row id is a counter; over a hundred-odd rows one of them is bound to equal the floor."""
    return [{k: v for k, v in dict(r).items() if k != "id"} for r in rows]


def test_neither_ledger_ever_carries_the_floor(store: Store) -> None:
    """Every offer from a lowball to a bid, each a fresh buyer on a fresh item, and then a buyer
    walking the whole ladder on one item. The offer that equals the floor is skipped: that number
    is the buyer's, and finding it would say nothing about whether the floor leaked."""
    for amount in range(1, 131):
        if amount == FLOOR:
            continue
        item = _item(store)
        _offer(store, item["id"], f"fb:{amount}", amount)
    walker = _item(store)
    for amount in (40, 55, 60, 65, 70, 72):
        _offer(store, walker["id"], "fb:walk", amount)

    assert store.untold_tells()
    assert leak_paths(_without_row_ids(_tell_rows(store)), FLOOR) == []
    assert leak_paths(_without_row_ids(store.untold_tells()), FLOOR) == []


def test_no_ledger_shape_has_anywhere_to_put_a_floor(store: Store) -> None:
    columns = {r["name"] for r in store._db.query("PRAGMA table_info(tells)")}
    assert not any("floor" in c for c in columns)
    for shape in (TellRecord, OfferDeclinedFacts, BuyerAnsweredFacts):
        assert not any("floor" in key for key in shape.__annotations__), shape.__name__


# --- buyer_answered tells ----------------------------------------------------------------------


def _sell_thread(store: Store, item_id: str, tid: str, handle="bob") -> None:
    store.create_thread(
        thread_id=tid, side="sell", market="fb", counterpart_handle=handle, item_id=item_id
    )


def _reply(
    store: Store,
    tid: str,
    text: str,
    *,
    kind="reply",
    in_msg_id=None,
    autonomous=True,
    now: float | None = None,
) -> str:
    cfg = pacing.resolve(_FAST, quiet_hours=(0, 0))
    reserved = store.reserve_reply(
        thread_id=tid,
        kind=kind,
        text=text,
        in_msg_id=in_msg_id,
        cfg=cfg,
        autonomous=autonomous,
        now=now,
    )
    store.commit_reply(
        intent_id=reserved["intent_id"],
        thread_id=tid,
        in_msg_id=in_msg_id,
        text=text,
        kind=kind,
        now=now,
    )
    return reserved["intent_id"]


def test_a_reply_to_a_buyer_leaves_an_fyi_with_both_sides_words(store: Store) -> None:
    item = _item(store, title="IKEA desk")
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    store.record_inbound("fb:1", msg_id="m1", text="hi", ts=10.0)
    store.record_inbound("fb:1", msg_id="m2", text="is it still available?", ts=20.0)

    _reply(store, "fb:1", "Yes, it's available. Want to buy it?")

    tells = store.untold_tells()
    assert len(tells) == 1
    tell = tells[0]
    assert (tell["kind"], tell["item_id"], tell["thread_id"]) == (
        "buyer_answered",
        item["id"],
        "fb:1",
    )
    assert tell["decision"] is False
    assert tell["facts"] == {
        "buyer_handle": "bob",
        "buyer_said": "is it still available?",
        "we_said": "Yes, it's available. Want to buy it?",
        "first_on_item": True,
        "item_title": "IKEA desk",
    }


def _firsts(store: Store) -> list[tuple]:
    return [(t["thread_id"], t["facts"]["first_on_item"]) for t in store.untold_tells()]


def test_only_the_first_buyer_answered_on_an_item_is_its_first(store: Store) -> None:
    """The flag is about the buyer, so every answer to the first buyer carries it and no answer
    to a later one does."""
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    _sell_thread(store, item["id"], "fb:2", handle="cara")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    _reply(store, "fb:1", "Yes!")
    store.record_inbound("fb:1", msg_id="m2", text="pickup where?", ts=30.0)
    _reply(store, "fb:1", "Tampines.")
    store.record_inbound("fb:2", msg_id="n1", text="still there?", ts=40.0)
    _reply(store, "fb:2", "It is.")

    assert _firsts(store) == [("fb:1", True), ("fb:1", True), ("fb:2", False)]


def test_the_first_buyer_stays_first_after_a_rival_is_answered(store: Store) -> None:
    """Answered first, then a rival, then the first buyer again: they are still the item's first
    buyer, and the rival still is not."""
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    _sell_thread(store, item["id"], "fb:2", handle="cara")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    _reply(store, "fb:1", "Yes!", now=1000.0)
    store.record_inbound("fb:2", msg_id="n1", text="still there?", ts=20.0)
    _reply(store, "fb:2", "It is.", now=2000.0)
    store.record_inbound("fb:1", msg_id="m2", text="pickup where?", ts=30.0)
    _reply(store, "fb:1", "Tampines.", now=3000.0)
    store.record_inbound("fb:2", msg_id="n2", text="can I come today?", ts=40.0)
    _reply(store, "fb:2", "Sure, after 6.", now=4000.0)

    assert _firsts(store) == [("fb:1", True), ("fb:2", False), ("fb:1", True), ("fb:2", False)]


def test_a_holding_line_first_does_not_take_the_first_buyer_away(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    store.record_inbound("fb:1", msg_id="m1", text="70 can?", ts=10.0)
    _reply(store, "fb:1", "Let me check and get right back to you!", kind="holding")
    _reply(store, "fb:1", "Can do 80.")

    assert _firsts(store) == [("fb:1", True)]


def test_a_rival_who_only_had_a_holding_line_was_not_answered_first(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    _sell_thread(store, item["id"], "fb:2", handle="cara")
    store.record_inbound("fb:1", msg_id="m1", text="70 can?", ts=10.0)
    _reply(store, "fb:1", "Let me check and get right back to you!", kind="holding")
    store.record_inbound("fb:2", msg_id="n1", text="still there?", ts=20.0)
    _reply(store, "fb:2", "It is.")

    assert _firsts(store) == [("fb:2", True)]


def test_buyers_on_other_items_do_not_count(store: Store) -> None:
    desk, lamp = _item(store, title="desk"), _item(store, title="lamp")
    _sell_thread(store, desk["id"], "fb:1")
    _sell_thread(store, lamp["id"], "fb:2")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    _reply(store, "fb:1", "Yes!")
    store.record_inbound("fb:2", msg_id="n1", text="available?", ts=20.0)
    _reply(store, "fb:2", "Yes!")

    assert _firsts(store) == [("fb:1", True), ("fb:2", True)]


def test_a_reply_with_no_buyer_message_behind_it_is_still_told(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    _reply(store, "fb:1", "Hi! Still keen on the bike?")

    (tell,) = store.untold_tells()
    assert tell["facts"]["buyer_said"] == ""
    assert tell["facts"]["we_said"] == "Hi! Still keen on the bike?"


@pytest.mark.parametrize("answered_msg_id", [None, "not-a-message-here"])
def test_with_no_answered_message_named_the_buyer_said_their_newest_words(
    store: Store, answered_msg_id
) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    store.record_inbound("fb:1", msg_id="m1", text="hi", ts=10.0)
    store.record_inbound("fb:1", msg_id="m2", text="is it still available?", ts=20.0)

    with store._db.transaction() as conn:
        updates_store.record_buyer_answered_in_txn(
            conn, thread_id="fb:1", answered_msg_id=answered_msg_id, we_said="Yes!", now=100.0
        )

    (tell,) = store.untold_tells()
    assert tell["facts"]["buyer_said"] == "is it still available?"


def test_a_holding_line_tells_the_seller_nothing(store: Store) -> None:
    """A holding line answered nothing — the question is still open and with the seller."""
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="70 can?", ts=10.0)
    _reply(store, "fb:1", "Let me check and get right back to you!", kind="holding")
    assert store.untold_tells() == []


def test_a_follow_up_nobody_asked_for_is_not_an_answer(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="hmm", ts=10.0)
    _reply(store, "fb:1", "Still keen?", kind="followup")
    assert store.untold_tells() == []


def test_a_reply_committed_twice_is_told_once(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    intent_id = _reply(store, "fb:1", "Yes!")
    store.commit_reply(
        intent_id=intent_id, thread_id="fb:1", in_msg_id=None, text="Yes!", kind="reply"
    )
    assert len(store.untold_tells()) == 1


def _reserved(store: Store, tid: str, text: str, *, autonomous=True) -> str:
    cfg = pacing.resolve(_FAST, quiet_hours=(0, 0))
    reserved = store.reserve_reply(
        thread_id=tid, kind="reply", text=text, in_msg_id="m1", cfg=cfg, autonomous=autonomous
    )
    return reserved["intent_id"]


def test_a_reply_settled_from_a_read_is_told_once(store: Store) -> None:
    """The send went unverified and the read lane later found it on the page. That is the reply
    committing, and a commit arriving after it is the same reply."""
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    intent_id = _reserved(store, "fb:1", "Yes!")

    assert store.settle_intent_from_read(intent_id, msg_id="fb-msg-1") is not None
    store.commit_reply(
        intent_id=intent_id, thread_id="fb:1", in_msg_id="m1", text="Yes!", kind="reply"
    )
    assert store.settle_intent_from_read(intent_id, msg_id="fb-msg-1") is None

    (tell,) = store.untold_tells()
    assert tell["kind"] == "buyer_answered"
    assert (tell["facts"]["buyer_said"], tell["facts"]["we_said"]) == ("available?", "Yes!")


def test_words_the_seller_sent_themselves_are_not_news_to_them(store: Store) -> None:
    """The seller dictated it, or sent it at their own terminal: they already know."""
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    _reply(store, "fb:1", "Yes, come by Saturday.", autonomous=False)
    assert _tell_rows(store) == []


def test_words_the_seller_sent_settled_from_a_read_are_not_news_either(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    intent_id = _reserved(store, "fb:1", "Yes, come by Saturday.", autonomous=False)

    assert store.settle_intent_from_read(intent_id, msg_id="fb-msg-1") is not None
    assert _tell_rows(store) == []


def test_a_rival_the_seller_answered_themselves_was_still_answered_first(store: Store) -> None:
    """Who composed the answer does not change that the other buyer had one."""
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    _sell_thread(store, item["id"], "fb:2", handle="cara")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    _reply(store, "fb:1", "Yes!", autonomous=False)
    store.record_inbound("fb:2", msg_id="n1", text="still there?", ts=20.0)
    _reply(store, "fb:2", "It is.")

    assert _firsts(store) == [("fb:2", False)]


def test_both_sides_words_are_kept_to_a_bounded_length(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="q" * 5000, ts=10.0)
    _reply(store, "fb:1", "a" * 5000)

    (tell,) = store.untold_tells()
    limit = updates_store._QUOTE_MAX_CHARS
    assert tell["facts"]["buyer_said"] == "q" * limit
    assert tell["facts"]["we_said"] == "a" * limit


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (updates_store._FIRST_ANSWER_SQL, ("fb:1",)),
        (updates_store._RIVAL_ANSWERED_SQL, ("item", "fb:1", 1000.0)),
    ],
    ids=["first_answer", "rival_answered"],
)
def test_finding_a_rival_answer_does_not_walk_every_send(store: Store, sql, params) -> None:
    """The lookups run inside every reply commit, so they must reach a thread's sends by index."""
    plan = store._db.query(f"EXPLAIN QUERY PLAN {sql}", params)
    details = [r["detail"] for r in plan]
    assert any("send_intents_thread" in d for d in details), details
    assert not any(d.startswith("SCAN") for d in details), details


class _Sink:
    """Takes the send. With `unverified`, the page took it but it could not be read back."""

    def __init__(self, store: Store, *, unverified=False):
        self.store = store
        self.unverified = unverified

    def send(self, thread, text, kind, intent_id):
        if self.unverified:
            self.store.mark_intent_sent_unverified(intent_id)
            raise RuntimeError("read-back failed")


@pytest.mark.parametrize("tier", ["pass:channel", "attended"])
def test_a_reply_the_seller_gave_us_to_send_is_not_told_back_to_them(
    make_ctx, store: Store, tier
) -> None:
    """The channel pass passes on the seller's own words, and an attended send is the seller at
    their terminal: either way they already know what the buyer was told."""
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    ctx = make_ctx(tier, reply_sink=_Sink(store), config=_FAST)

    res = dispatch("send_reply", {"thread_id": "fb:1", "text": "Yes, Saturday works."}, ctx)

    assert res["status"] == "sent"
    assert _tell_rows(store) == []


def test_a_reply_lane_answer_found_later_on_the_page_is_told_once(make_ctx, store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1", handle="bob")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    scope = Scope.of(threads=["fb:1"], items=[item["id"]])
    ctx = make_ctx(
        "pass:reply",
        pass_id="p1",
        scope=scope,
        reply_sink=_Sink(store, unverified=True),
        config=_FAST,
    )

    res = dispatch("send_reply", {"thread_id": "fb:1", "text": "Yes!", "in_msg_id": "m1"}, ctx)
    assert res["status"] == "send_unverified" and _tell_rows(store) == []
    assert store.settle_intent_from_read(res["intent_id"], msg_id="fb-msg-1") is not None
    assert store.settle_intent_from_read(res["intent_id"], msg_id="fb-msg-1") is None

    (tell,) = store.untold_tells()
    assert tell["kind"] == "buyer_answered"
    assert (tell["facts"]["buyer_said"], tell["facts"]["we_said"]) == ("available?", "Yes!")


# --- buyers_updated tells ----------------------------------------------------------------------


def _update(store: Store, tid: str, *, price_drop=True, new_photos=False, commit=True) -> str:
    """Reserve, and unless told otherwise commit, a past-enquirer update on `tid`."""
    cfg = pacing.resolve(_FAST, quiet_hours=(0, 0))
    reserved = store.reserve_reply(
        thread_id=tid,
        kind="followup",
        text="The price is now $80 (was $95).",
        in_msg_id="m1",
        cfg=cfg,
        announces=updates_store.announcement(price_drop=price_drop, new_photos=new_photos),
    )
    if commit:
        store.commit_reply(
            intent_id=reserved["intent_id"],
            thread_id=tid,
            in_msg_id="m1",
            text="The price is now $80 (was $95).",
            kind="followup",
        )
    return reserved["intent_id"]


@pytest.mark.parametrize(("price_drop", "new_photos"), [(True, False), (False, True), (True, True)])
def test_a_committed_update_tells_the_seller_of_one_buyer(
    store: Store, price_drop, new_photos
) -> None:
    item = _item(store, title="Teak lamp")
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)

    _update(store, "fb:1", price_drop=price_drop, new_photos=new_photos)

    (tell,) = store.untold_tells()
    assert (tell["kind"], tell["item_id"], tell["thread_id"]) == (
        "buyers_updated",
        item["id"],
        None,
    )
    assert tell["facts"] == {
        "buyers": 1,
        "price_drop": price_drop,
        "new_photos": new_photos,
        "item_title": "Teak lamp",
    }
    assert _update_stamp(store, "fb:1") is not None


def test_an_update_committed_twice_is_told_once(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    intent_id = _update(store, "fb:1")
    store.commit_reply(
        intent_id=intent_id, thread_id="fb:1", in_msg_id="m1", text="x", kind="followup"
    )
    assert store.settle_intent_from_read(intent_id, msg_id="fb-msg-1") is None
    assert len(store.untold_tells()) == 1


def test_an_update_settled_from_a_read_is_told_once(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    intent_id = _update(store, "fb:1", commit=False)
    assert store.untold_tells() == []

    assert store.settle_intent_from_read(intent_id, msg_id="fb-msg-1") is not None
    store.commit_reply(
        intent_id=intent_id, thread_id="fb:1", in_msg_id="m1", text="x", kind="followup"
    )
    assert store.settle_intent_from_read(intent_id, msg_id="fb-msg-1") is None

    assert [t["facts"]["buyers"] for t in store.untold_tells()] == [1]


def test_an_update_never_sent_tells_nobody(store: Store) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    store.drop_refused_intent(_update(store, "fb:1", commit=False))
    assert _tell_rows(store) == []


def test_an_ordinary_follow_up_is_not_an_update(store: Store) -> None:
    """Only a follow-up reserved as announcing a change is one; any other nudge is not."""
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="hmm", ts=10.0)
    _reply(store, "fb:1", "Still keen?", kind="followup")
    assert _tell_rows(store) == []
    assert _update_stamp(store, "fb:1") is None


def _update_stamp(store: Store, tid: str) -> float | None:
    return store._db.query(  # noqa: SLF001 — not a thread field the model reads
        "SELECT last_update_ts FROM threads WHERE thread_id = ?", (tid,)
    )[0]["last_update_ts"]


@pytest.mark.parametrize("tier", ["pass:channel", "attended"])
def test_a_follow_up_the_model_writes_is_never_an_update(make_ctx, store: Store, tier) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="hmm", ts=10.0)
    ctx = make_ctx(tier, reply_sink=_Sink(store), config=_FAST)

    params = {"thread_id": "fb:1", "text": "Price dropped!", "kind": "followup"}
    assert dispatch("send_reply", params, ctx)["status"] == "sent"
    assert _tell_rows(store) == []


def test_announcing_nothing_marks_no_update() -> None:
    assert updates_store.announcement(price_drop=False, new_photos=False) is None


def _failing_tell_insert(*_args, **_kwargs) -> None:
    raise RuntimeError("tell insert failed")


def test_a_reply_whose_tell_fails_commits_nothing(store: Store, monkeypatch) -> None:
    item = _item(store)
    _sell_thread(store, item["id"], "fb:1")
    store.record_inbound("fb:1", msg_id="m1", text="available?", ts=10.0)
    intent_id = _reserved(store, "fb:1", "Yes!")
    monkeypatch.setattr(updates_store, "_insert_tell", _failing_tell_insert)

    with pytest.raises(RuntimeError):
        store.commit_reply(
            intent_id=intent_id, thread_id="fb:1", in_msg_id="m1", text="Yes!", kind="reply"
        )

    outbound = store._db.query("SELECT 1 FROM thread_messages WHERE dir = 'out'")
    (intent,) = store._db.query("SELECT status FROM send_intents WHERE intent_id = ?", (intent_id,))
    assert list(outbound) == [] and intent["status"] == "pending" and _tell_rows(store) == []


def test_an_offer_whose_tell_fails_decides_nothing(store: Store, monkeypatch) -> None:
    item = _item(store)
    monkeypatch.setattr(updates_store, "_insert_tell", _failing_tell_insert)

    with pytest.raises(RuntimeError):
        _offer(store, item["id"], "fb:1", 30)

    assert _tell_rows(store) == []
    assert store._db.query("SELECT 1 FROM negotiations") == []


def test_a_reply_on_the_buying_side_is_not_about_a_listing(store: Store) -> None:
    want = store.create_want(query="lamp")
    store.create_thread(
        thread_id="fb:buy",
        side="buy",
        market="fb",
        counterpart_handle="sam",
        want_id=want["want_id"],
    )
    store.record_inbound("fb:buy", msg_id="m1", text="yes still have", ts=10.0)
    _reply(store, "fb:buy", "Great, can I pick up today?")
    assert store.untold_tells() == []


# --- reading and stamping ----------------------------------------------------------------------


def test_untold_tells_come_oldest_first_and_stamping_takes_them_off(store: Store) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:1", 30, handle="ann")
    _offer(store, item["id"], "fb:2", 40, handle="ben")
    _offer(store, item["id"], "fb:3", 45, handle="cal")
    first, second, third = store.untold_tells()
    assert [t["facts"]["buyer_handle"] for t in (first, second, third)] == ["ann", "ben", "cal"]

    stamped = store.mark_tells_told([first["id"], second["id"]], notice_id=42)

    assert stamped == 2
    assert [t["id"] for t in store.untold_tells()] == [third["id"]]
    rows = {r["id"]: r for r in _tell_rows(store)}
    assert rows[first["id"]]["notice_id"] == 42 and rows[first["id"]]["told_ts"] is not None
    assert rows[third["id"]]["notice_id"] is None and rows[third["id"]]["told_ts"] is None


def test_a_tell_is_stamped_once(store: Store) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:1", 30)
    (tell,) = store.untold_tells()
    assert store.mark_tells_told([tell["id"]], notice_id=7) == 1
    assert store.mark_tells_told([tell["id"]], notice_id=8) == 0
    assert _tell_rows(store)[0]["notice_id"] == 7


def test_a_tell_can_be_retired_without_a_notice(store: Store) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:1", 30)
    (tell,) = store.untold_tells()
    assert store.mark_tells_told([tell["id"]], notice_id=None) == 1
    assert store.untold_tells() == []


def test_stamping_nothing_stamps_nothing(store: Store) -> None:
    assert store.mark_tells_told([], notice_id=1) == 0
