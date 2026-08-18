"""Buy-side negotiation + set_budget: the open/accept/climb/walk ladder, the never-exceeds-max
sweep, seed-then-resume monotonicity, stand-down when committed elsewhere, and the budget writer's
floor-mirroring discipline (no value ever leaked)."""

from __future__ import annotations

import pytest

from sellee.store import Store, StoreError


def _want(store: Store, *, max_budget=None, target=None, step=None, rounds=None, ratio=None):
    want = store.create_want(query="iPhone 15")
    if max_budget is not None:
        store.set_budget(
            want["want_id"],
            max_budget,
            "buyer",
            target_price=target,
            currency="SGD",
            opening_ratio=ratio,
            auto_counter_step=step,
            auto_counter_rounds=rounds,
        )
    return want


# --- set_budget discipline --------------------------------------------------------------------


def test_budget_ack_carries_no_value(store: Store) -> None:
    want = store.create_want(query="thing")
    ack = store.set_budget(want["want_id"], 500.0, "buyer", target_price=400.0, currency="SGD")
    assert ack == {
        "status": "written",
        "want_id": want["want_id"],
        "source": "buyer",
        "replaced": None,
    }
    assert "max_budget" not in ack and "target_price" not in ack


def test_budget_rejects_target_above_max(store: Store) -> None:
    want = store.create_want(query="thing")
    with pytest.raises(StoreError, match="at or below max"):
        store.set_budget(want["want_id"], 100.0, "buyer", target_price=150.0)


def test_budget_default_never_clobbers_buyer(store: Store) -> None:
    want = store.create_want(query="thing")
    store.set_budget(want["want_id"], 500.0, "buyer")
    with pytest.raises(StoreError):
        store.set_budget(want["want_id"], 400.0, "default")
    # force replaces a buyer value with another buyer value
    ack = store.set_budget(want["want_id"], 450.0, "buyer", force=True)
    assert ack["replaced"] == "buyer"


def test_budget_missing_want_refused(store: Store) -> None:
    with pytest.raises(StoreError):
        store.set_budget("want_nope", 500.0, "buyer")


def test_no_tool_read_returns_the_budget(store: Store) -> None:
    want = _want(store, max_budget=500.0, target=400.0)
    got = store.get_want(want["want_id"])
    assert "max_budget" not in got and "budget" not in got
    # the engine-only reader does have it (never reachable by a tool)
    assert store.get_budget(want["want_id"])["max_budget"] == 500.0


# --- open / reply ladder ----------------------------------------------------------------------


def test_open_makes_offer_below_list_under_budget(store: Store) -> None:
    want = _want(store, max_budget=500.0, target=400.0, ratio=0.8)
    res = store.buyer_negotiate_open(want["want_id"], "cl:1", "sue", 600.0)
    assert res["decision"] == "opening_offer"
    assert res["offer_price"] <= 500.0  # never above the secret max


def test_open_accepts_when_listing_already_cheap(store: Store) -> None:
    # a listing at/under what we'd open with (opening >= listed) and within budget → just take it
    want = _want(store, max_budget=500.0, target=480.0, ratio=1.0)
    res = store.buyer_negotiate_open(want["want_id"], "cl:1", "sue", 450.0)
    assert res["decision"] == "accept" and res["accept_price"] == 450


def test_reply_climbs_capped_then_walks(store: Store) -> None:
    want = _want(store, max_budget=300.0, target=250.0, step=20, rounds=2)
    store.buyer_negotiate_open(want["want_id"], "cl:1", "sue", 400.0)
    # seller stays well above budget → we climb, strictly under 300, then walk once rounds spent
    prices = []
    r1 = store.buyer_negotiate_reply(want["want_id"], "cl:1", 380.0)
    prices.append(r1)
    r2 = store.buyer_negotiate_reply(want["want_id"], "cl:1", 380.0)
    prices.append(r2)
    r3 = store.buyer_negotiate_reply(want["want_id"], "cl:1", 380.0)
    prices.append(r3)
    for r in prices:
        if r.get("offer_price") is not None:
            assert r["offer_price"] < 300  # strictly under the ceiling
    assert prices[-1]["decision"] == "walk_away"


def test_reply_accepts_within_budget(store: Store) -> None:
    want = _want(store, max_budget=500.0, target=400.0, step=20, rounds=2)
    store.buyer_negotiate_open(want["want_id"], "cl:1", "sue", 600.0)
    res = store.buyer_negotiate_reply(want["want_id"], "cl:1", 380.0)  # at/below target → accept
    assert res["decision"] == "accept" and res["accept_price"] == 380


@pytest.mark.parametrize("seller_price", list(range(50, 700, 25)))
def test_never_emits_above_max(store: Store, seller_price) -> None:
    want = _want(store, max_budget=300.0, target=200.0, step=20, rounds=5)
    store.buyer_negotiate_open(want["want_id"], "cl:1", "sue", 650.0)
    res = store.buyer_negotiate_reply(want["want_id"], "cl:1", float(seller_price))
    for key in ("offer_price", "accept_price"):
        if res.get(key) is not None:
            assert res[key] <= 300


# --- seed / accept / walk ---------------------------------------------------------------------


def test_seed_then_resume_is_monotonic(store: Store) -> None:
    want = _want(store, max_budget=500.0, target=400.0, step=20, rounds=3)
    store.buyer_negotiate_seed(want["want_id"], "cl:1", "sue", listed_price=600.0, our_last=420.0)
    # a real reply climbs from the seeded 420, never below it
    res = store.buyer_negotiate_reply(want["want_id"], "cl:1", 480.0)
    if res.get("offer_price") is not None:
        assert res["offer_price"] >= 420


def test_accept_commits_and_closes_siblings(store: Store) -> None:
    want = _want(store, max_budget=500.0, target=400.0)
    store.buyer_negotiate_open(want["want_id"], "cl:1", "sue", 600.0)
    store.buyer_negotiate_open(want["want_id"], "fb:2", "bob", 600.0)
    res = store.buyer_negotiate_accept(want["want_id"], "cl:1")
    assert res["want_state"] == "committed"
    assert res["close_threads"] == ["fb:2"]  # the sibling is closed


def test_stand_down_when_committed_elsewhere(store: Store) -> None:
    want = _want(store, max_budget=500.0, target=400.0)
    store.buyer_negotiate_open(want["want_id"], "cl:1", "sue", 600.0)
    store.buyer_negotiate_open(want["want_id"], "fb:2", "bob", 600.0)
    store.buyer_negotiate_accept(want["want_id"], "cl:1")
    # a reply on the now-losing thread stands down, never negotiates
    res = store.buyer_negotiate_reply(want["want_id"], "fb:2", 450.0)
    assert res["decision"] == "stand_down"


def test_open_without_budget_errors(store: Store) -> None:
    want = store.create_want(query="thing")  # no budget set
    with pytest.raises(StoreError, match="no budget"):
        store.buyer_negotiate_open(want["want_id"], "cl:1", "sue", 600.0)
