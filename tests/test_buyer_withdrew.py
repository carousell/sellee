"""A buyer backing out is marked withdrawn, an item they held is released, and the store queues one
notice for the seller in the same transaction."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sellee.config import Config
from sellee.store import Scope, Store, StoreError
from sellee.tools.registry import ToolError, dispatch

CFG = Config()
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
_PROPERTY = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
BUYERS = ("fb:a", "fb:b", "fb:c")


def _text(result: dict) -> str:
    return "backed out"


def _item(store: Store, *, list_price=100.0):
    item = store.create_item(title="Scarf", list_price=list_price, currency="USD")
    store.set_floor(item["id"], list_price / 2, "seller")
    return item


def _offer(store, item_id, thread_id, offer):
    return store.negotiate_offer(item_id, thread_id, thread_id, offer, config=CFG)


# A ledger: some offers from up to three buyers, maybe a seller-confirmed leading bid, and the
# buyer who then backs out.
_LEDGERS = st.tuples(
    st.lists(st.tuples(st.integers(0, 2), st.sampled_from([60, 80, 100, 120, 150])), max_size=6),
    st.booleans(),
    st.integers(0, 2),
)


def _build(store: Store, offers, confirm) -> dict:
    item = _item(store)
    for who, price in offers:
        _offer(store, item["id"], BUYERS[who], price)
    if confirm:
        front = store.negotiate_status(item["id"])["front_runner"]
        if front and front["kind"] == "bid":
            store.negotiate_confirm_bid(item["id"], front["thread_id"])
    return item


def _notice_count(store: Store) -> int:
    return len(store.list_queued_notices())


# --- properties --------------------------------------------------------------------------------


@_PROPERTY
@given(ledger=_LEDGERS)
def test_a_holder_who_withdraws_puts_the_item_back_on_the_market(store, ledger) -> None:
    offers, confirm, who = ledger
    item = _build(store, offers, confirm)
    before = store.negotiate_status(item["id"])
    held = (before["front_runner"] or {}).get("thread_id") == BUYERS[who]

    result = store.negotiate_withdraw(item["id"], BUYERS[who], notice=_text)

    after = store.negotiate_status(item["id"])
    assert result["was_holder"] is held
    if held:
        assert after["item_state"] in ("open", "bidding")
        assert after["front_runner"] is None
    else:
        assert after["item_state"] == before["item_state"]
        assert after["front_runner"] == before["front_runner"]


@_PROPERTY
@given(ledger=_LEDGERS)
def test_a_withdrawal_touches_only_the_buyer_who_withdrew(store, ledger) -> None:
    offers, confirm, who = ledger
    item = _build(store, offers, confirm)
    before = store.negotiate_status(item["id"])["buyers"]

    store.negotiate_withdraw(item["id"], BUYERS[who], notice=_text)

    after = store.negotiate_status(item["id"])["buyers"]
    assert {t: b for t, b in after.items() if t != BUYERS[who]} == {
        t: b for t, b in before.items() if t != BUYERS[who]
    }
    if BUYERS[who] in before:
        assert after[BUYERS[who]]["status"] == "withdrew"
    else:
        assert BUYERS[who] not in after


@_PROPERTY
@given(ledger=_LEDGERS)
def test_the_seller_is_told_once_and_only_about_a_buyer_who_offered(store, ledger) -> None:
    offers, confirm, who = ledger
    item = _build(store, offers, confirm)
    offered = BUYERS[who] in store.negotiate_status(item["id"])["buyers"]
    count = _notice_count(store)

    first = store.negotiate_withdraw(item["id"], BUYERS[who], notice=_text)
    second = store.negotiate_withdraw(item["id"], BUYERS[who], notice=_text)

    assert _notice_count(store) == count + (1 if offered else 0)
    assert first["withdrew"] is offered
    assert second["withdrew"] is False


@_PROPERTY
@given(ledger=_LEDGERS)
def test_a_buyer_who_never_offered_withdraws_silently(store, ledger) -> None:
    offers, confirm, _ = ledger
    item = _build(store, offers, confirm)
    before = store.negotiate_status(item["id"])
    count = _notice_count(store)

    result = store.negotiate_withdraw(item["id"], "fb:only-asked", notice=_text)

    assert result == {
        "withdrew": False,
        "was_holder": False,
        "item_state": before["item_state"],
        "notice_id": None,
    }
    assert store.negotiate_status(item["id"]) == before
    assert _notice_count(store) == count


# --- examples ----------------------------------------------------------------------------------


def test_a_withdrawn_offer_no_longer_outbids_anyone(store) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:a", 150)
    store.negotiate_withdraw(item["id"], "fb:a", notice=_text)
    assert _offer(store, item["id"], "fb:b", 120)["decision"] == "bid_lead"


def test_a_sold_item_cannot_be_withdrawn_from(store) -> None:
    item = _item(store)
    _offer(store, item["id"], "fb:a", 100)
    store.negotiate_confirm_sold(item["id"], "fb:a")
    with pytest.raises(StoreError, match="sold"):
        store.negotiate_withdraw(item["id"], "fb:a", notice=_text)


def _scarf_thread(store, tid="fb:ks5qn", handle="ks5qn"):
    item = _item(store, list_price=12.0)
    store.create_thread(
        thread_id=tid, side="sell", market="fb", counterpart_handle=handle, item_id=item["id"]
    )
    return item


def _reply_ctx(make_ctx, item, tid="fb:ks5qn", **kw):
    scope = Scope.of(threads=[tid], items=[item["id"]])
    return make_ctx("pass:reply", pass_id="p1", scope=scope, config=_FAST, **kw)


def test_the_seller_is_told_who_backed_out_and_that_it_is_back_on_the_market(
    make_ctx, store
) -> None:
    item = _scarf_thread(store)
    _offer(store, item["id"], "fb:ks5qn", 20)  # above list: a leading bid
    store.negotiate_confirm_bid(item["id"], "fb:ks5qn")

    result = dispatch(
        "buyer_withdrew",
        {"thread_id": "fb:ks5qn", "reason": "shipping too high\nfor them"},
        _reply_ctx(make_ctx, item),
    )

    assert result["withdrew"] is True and result["was_holder"] is True
    (notice,) = store.list_queued_notices()
    assert "ks5qn" in notice["text"] and "Scarf" in notice["text"]
    assert "shipping too high for them" in notice["text"]
    assert "It's back on the market." in notice["text"]
    assert notice["ref"] == "buyer-withdrew:fb:ks5qn"
    assert notice["pass_id"] is None  # a background report, not a pass answering the seller


def test_a_buyer_who_did_not_hold_the_item_is_reported_without_the_market_line(
    make_ctx, store
) -> None:
    item = _scarf_thread(store)
    _offer(store, item["id"], "fb:ks5qn", 5)  # a lowball, deflected
    assert store.negotiate_status(item["id"])["front_runner"] is None

    dispatch(
        "buyer_withdrew",
        {"thread_id": "fb:ks5qn", "reason": "found another one"},
        _reply_ctx(make_ctx, item),
    )

    (notice,) = store.list_queued_notices()
    assert "found another one" in notice["text"]
    assert "back on the market" not in notice["text"]


def test_a_reply_pass_withdraws_the_buyer_then_signs_off(make_ctx, store) -> None:
    """The order a reply pass takes on "nvm i found another scarf"."""

    class Sink:
        sends: list = []

        def send(self, thread, text, kind, intent_id):
            self.sends.append(text)

    item = _scarf_thread(store)
    _offer(store, item["id"], "fb:ks5qn", 12)
    store.record_inbound("fb:ks5qn", msg_id="m1", text="nvm i found another scarf", ts=100.0)
    ctx = _reply_ctx(make_ctx, item, reply_sink=Sink())

    withdrew = dispatch(
        "buyer_withdrew", {"thread_id": "fb:ks5qn", "reason": "found another scarf"}, ctx
    )
    sent = dispatch(
        "send_reply",
        {
            "thread_id": "fb:ks5qn",
            "text": "No worries, thanks for letting me know!",
            "in_msg_id": "m1",
        },
        ctx,
    )

    assert withdrew["withdrew"] is True
    assert sent["status"] == "sent"
    assert store.negotiate_status(item["id"])["buyers"]["fb:ks5qn"]["status"] == "withdrew"


def test_a_reply_pass_cannot_withdraw_a_buyer_outside_its_scope(make_ctx, store) -> None:
    mine = _scarf_thread(store)
    other = _scarf_thread(store, tid="fb:other", handle="other")
    _offer(store, other["id"], "fb:other", 12)

    with pytest.raises(ToolError):
        dispatch(
            "buyer_withdrew",
            {"thread_id": "fb:other", "reason": "x"},
            _reply_ctx(make_ctx, mine),
        )
    assert store.negotiate_status(other["id"])["buyers"]["fb:other"]["status"] != "withdrew"


def test_the_buyer_rulebook_names_withdrawal() -> None:
    from sellee import skills

    text = skills.load("buyer-conversation")
    assert "buyer_withdrew" in text
    for example in ("nvm i found another scarf", "I'll pass", "let me think about it"):
        assert example in text
