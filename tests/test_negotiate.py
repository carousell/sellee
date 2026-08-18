"""Sell-side negotiation: the Harry guard, FCFS single-inventory, the bidding bar, the below-list
floor sweep (no counter ever below the floor, and no floor value leaked), stale-counter vs
other_best, the needs_floor lazy hold, floorless default-floor writes, and confirm/sold/release."""

from __future__ import annotations

import concurrent.futures

import pytest

from sellee.config import Config
from sellee.store import Store, StoreError

CFG = Config()


def _item(store: Store, *, list_price=100.0, floor=None, floor_source="seller"):
    item = store.create_item(title="Thing", list_price=list_price, currency="SGD")
    if floor is not None:
        store.set_floor(item["id"], floor, floor_source)
    return item


def _offer(store, item_id, thread_id, offer, handle="buyer"):
    return store.negotiate_offer(item_id, thread_id, handle, offer, config=CFG)


# --- Harry guard: above list never auto-accepts -----------------------------------------------


def test_above_list_returns_bid_lead_needs_confirm(store: Store) -> None:
    item = _item(store, list_price=100.0, floor=50.0)
    res = _offer(store, item["id"], "fb:1", 200)
    assert res["decision"] == "bid_lead"
    assert res["needs_seller_confirm"] is True
    assert res["leading_amount"] == 200
    assert res["item_state"] == "bidding"


# --- FCFS single inventory --------------------------------------------------------------------


def test_fcfs_at_list_single_winner_under_concurrency(store: Store) -> None:
    for _ in range(6):
        item = _item(store, list_price=100.0, floor=50.0)

        def bid(tid, item_id=item["id"]):
            return _offer(store, item_id, tid, 100, handle=tid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            a = ex.submit(bid, "fb:a")
            b = ex.submit(bid, "fb:b")
            results = [a.result(), b.result()]
        decisions = sorted(r["decision"] for r in results)
        # exactly one wins the item at list; the other is told it is pending
        assert decisions == ["accept_fcfs", "fcfs_taken"]


def test_second_buyer_below_list_after_fcfs_is_blocked(store: Store) -> None:
    item = _item(store, list_price=100.0, floor=50.0)
    _offer(store, item["id"], "fb:a", 100)  # a takes it at list
    res = _offer(store, item["id"], "fb:b", 80)  # b tries to haggle
    assert res["decision"] == "fcfs_taken"


# --- below-list floor sweep -------------------------------------------------------------------


@pytest.mark.parametrize("offer", list(range(1, 100)))
def test_no_counter_or_accept_ever_below_floor(store: Store, offer) -> None:
    item = _item(store, list_price=100.0, floor=50.0)
    res = _offer(store, item["id"], "fb:1", offer)
    for key in ("counter_price", "accept_price"):
        if res.get(key) is not None:
            assert res[key] >= 50, f"offer {offer} produced {key}={res[key]} below floor 50"


def test_stale_lower_offer_never_undercuts_other_best(store: Store) -> None:
    item = _item(store, list_price=100.0, floor=50.0)
    _offer(store, item["id"], "fb:high", 90)  # a standing offer at 90
    # buyer B meets a stale counter of 85 — must NOT lock the item below other_best+1 (91)
    res = _offer(store, item["id"], "fb:low", 85)
    for key in ("counter_price", "accept_price"):
        if res.get(key) is not None:
            assert res[key] >= 91


# --- needs_floor lazy hold --------------------------------------------------------------------


def test_below_list_with_no_floor_holds_for_floor(store: Store) -> None:
    item = _item(store, list_price=100.0)  # no floor set
    res = _offer(store, item["id"], "fb:1", 70)
    assert res["decision"] == "needs_floor"
    assert res["needs_seller_confirm"] is True
    # nothing numeric leaked, and no default floor was written on the below-list path
    assert res.get("counter_price") is None
    assert store.get_floor(item["id"]) is None


def test_at_or_above_list_with_no_floor_writes_default_floor(store: Store) -> None:
    item = _item(store, list_price=100.0)  # no floor set
    res = _offer(store, item["id"], "fb:1", 100)
    assert res["decision"] == "accept_fcfs"
    floor = store.get_floor(item["id"])
    assert floor["source"] == "default" and floor["floor"] == 100.0


def test_needs_floor_hold_bars_a_rival_once_floor_lands(store: Store) -> None:
    item = _item(store, list_price=100.0)  # no floor
    _offer(store, item["id"], "fb:high", 80)  # held for floor at 80
    store.set_floor(item["id"], 50.0, "seller")
    # a rival's low offer must not counter below the held 80 + 1
    res = _offer(store, item["id"], "fb:low", 60)
    for key in ("counter_price", "accept_price"):
        if res.get(key) is not None:
            assert res[key] >= 81


def test_no_valid_list_price_is_a_data_error(store: Store) -> None:
    item = store.create_item(title="NoPrice", list_price=0)  # invalid list price, no floor
    with pytest.raises(StoreError):
        _offer(store, item["id"], "fb:1", 50)


# --- confirm / sold / release -----------------------------------------------------------------


def test_confirm_bid_wrong_thread_refused(store: Store) -> None:
    item = _item(store, list_price=100.0, floor=50.0)
    _offer(store, item["id"], "fb:leader", 200)  # leader bids above list
    with pytest.raises(StoreError, match="leading bid"):
        store.negotiate_confirm_bid(item["id"], "fb:someone_else")
    # the true leader confirms fine
    ok = store.negotiate_confirm_bid(item["id"], "fb:leader")
    assert ok["reserved_for"] == "fb:leader" and ok["item_state"] == "reserved_provisional"


def test_confirm_sold_take_down_and_close_lists(store: Store) -> None:
    item = _item(store, list_price=100.0, floor=50.0)
    store.record_listing_url(item["id"], "fb", "https://www.facebook.com/marketplace/item/1")
    store.record_listing_url(item["id"], "carousell", "https://www.carousell.sg/p/1")
    _offer(store, item["id"], "fb:winner", 100)
    _offer(store, item["id"], "cl:loser", 80)  # a below-list haggler on the other market
    sold = store.negotiate_confirm_sold(item["id"], "fb:winner")
    assert sold["item_state"] == "sold"
    # the won platform's own listing stays; the other market's listing is taken down
    platforms = {t["platform"] for t in sold["take_down"]}
    assert platforms == {"carousell"}
    assert "cl:loser" in sold["close_threads"]


def test_release_returns_bidding_item_to_bidding(store: Store) -> None:
    item = _item(store, list_price=100.0, floor=50.0)
    _offer(store, item["id"], "fb:leader", 200)  # a bidding item
    store.negotiate_confirm_bid(item["id"], "fb:leader")
    released = store.negotiate_release(item["id"])
    assert released["item_state"] == "bidding"  # a bidding item returns to bidding, not open


def test_release_fcfs_item_to_open(store: Store) -> None:
    item = _item(store, list_price=100.0, floor=50.0)
    _offer(store, item["id"], "fb:a", 100)  # FCFS reserve
    released = store.negotiate_release(item["id"])
    assert released["item_state"] == "open"
