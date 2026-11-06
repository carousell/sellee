"""The slow tells the update lane writes itself — a buyer left waiting on the seller's answer, a
listing nobody has written about, a sale the seller said they would close themselves — and the
weekly line for a seller with nothing else to hear.

Each writer is guarded by the ledger alone, so these run it more than once over the same rows and
check it wrote once; then that a tell whose premise went away is retired untold, and the gates on
the weekly line: the seller's own Saturday, a quiet week, something live, no update on its way,
once per week.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from tests.conftest import leak_paths, seed_setting

from sellee import skills
from sellee.channel import activity
from sellee.config import Config
from sellee.store import Store
from sellee.tools import TIER_ATTENDED
from sellee.tools.registry import ToolError, all_specs, dispatch
from sellee.updates import CHECKIN_OPTIONS

MINUTE = 60.0
HOUR = 3600.0
DAY = 24 * HOUR
FLOOR = 73.0
# Saturday 2026-11-07 05:00 UTC: already Saturday evening on Kiritimati (UTC+14), still Friday
# evening in Pago Pago (UTC-11).
SATURDAY_UTC_MORNING = datetime(2026, 11, 7, 5, 0, tzinfo=timezone.utc).timestamp()
SATURDAY_ZONE = "Pacific/Kiritimati"
FRIDAY_ZONE = "Pacific/Pago_Pago"


def _bind(store: Store) -> None:
    store.arm_bind("sellee_bot", "n1")
    store.complete_bind(555, update_offset=1, nonce=store.get_channel()["bind_nonce"])


def _live_item(store: Store, title: str = "IKEA desk") -> str:
    item = store.create_item(title=title, list_price=100.0, currency="USD")
    store.set_floor(item["id"], FLOOR, "seller")
    store.record_listing_url(
        item["id"], "fb", f"https://www.facebook.com/marketplace/item/{item['id']}"
    )
    return item["id"]


def _live_since(store: Store, item_id: str, ts: float) -> None:
    with store._db.transaction() as conn:  # noqa: SLF001 — tests arrange ledger rows directly
        conn.execute("UPDATE items SET live_since_ts = ? WHERE id = ?", (ts, item_id))


def _sell_thread(store: Store, item_id: str, tid: str = "fb:1", handle: str = "bob_k") -> str:
    store.create_thread(
        thread_id=tid, side="sell", market="fb", counterpart_handle=handle, item_id=item_id
    )
    return tid


def _escalate(store: Store, tid: str, *, opened: float) -> str:
    esc = store.escalate(tid, open_question=f"bob offered 60; your minimum is {FLOOR:g}. Accept?")
    with store._db.transaction() as conn:  # noqa: SLF001
        conn.execute("UPDATE escalations SET created_ts = ? WHERE id = ?", (opened, esc["id"]))
    return esc["id"]


def _tells(store: Store, kind: str) -> list[dict]:
    rows = store._db.query(  # noqa: SLF001
        "SELECT * FROM tells WHERE kind = ? ORDER BY id", (kind,)
    )
    return [dict(r, facts=json.loads(r["facts"])) for r in rows]


def _notices(store: Store, prefix: str) -> list[dict]:
    rows = store._db.query(  # noqa: SLF001
        "SELECT * FROM notices WHERE ref LIKE ? ORDER BY id", (f"{prefix}%",)
    )
    return [dict(r) for r in rows]


def _hand_over(make_ctx, store: Store, tid: str, *, at: float) -> None:
    esc = store.escalate(tid, open_question="How do you want to close?")
    dispatch(
        "resolve_escalation",
        {"escalation_id": esc["id"], "resolution": "seller handling", "seller_handles_it": True},
        make_ctx(TIER_ATTENDED),
    )
    with store._db.transaction() as conn:  # noqa: SLF001
        conn.execute("UPDATE seller_handled_deals SET handed_ts = ? WHERE thread_id = ?", (at, tid))


@pytest.fixture
def seller(store: Store) -> Store:
    _bind(store)
    return store


# --- a buyer waiting on the seller ------------------------------------------------------------


def test_an_escalation_open_past_twelve_hours_is_told_once(store: Store) -> None:
    item = _live_item(store, "Trek Emonda 54cm")
    tid = _sell_thread(store, item)
    store.record_inbound(tid, msg_id="m1", text="does it come with pedals?", ts=10.0)
    now = 1_000_000.0
    _escalate(store, tid, opened=now - 13 * HOUR)

    assert store.write_slow_tells(now=now) == 1
    assert store.write_slow_tells(now=now + HOUR) == 0

    (tell,) = _tells(store, "ask_waiting")
    assert tell["decision"] == 1  # it asks the seller for something, so `decisions` keeps it
    assert (tell["item_id"], tell["thread_id"]) == (item, tid)
    assert tell["facts"] == {
        "buyer_handle": "bob_k",
        "buyer_said": "does it come with pedals?",
        "asked_ts": now - 13 * HOUR,
        "item_title": "Trek Emonda 54cm",
    }
    # the escalation's own question is ours to the seller and may name the minimum; it stays out
    assert leak_paths(tell, FLOOR) == []


def test_an_escalation_younger_than_twelve_hours_is_not_told(store: Store) -> None:
    tid = _sell_thread(store, _live_item(store))
    now = 1_000_000.0
    _escalate(store, tid, opened=now - 11 * HOUR)

    assert store.write_slow_tells(now=now) == 0


def test_a_new_escalation_on_the_same_thread_is_told_again(store: Store) -> None:
    tid = _sell_thread(store, _live_item(store))
    now = 1_000_000.0
    first = _escalate(store, tid, opened=now - 13 * HOUR)
    store.write_slow_tells(now=now)
    store.resolve_escalation(first, "answered")
    _escalate(store, tid, opened=now + HOUR)

    assert store.write_slow_tells(now=now + 14 * HOUR) == 1
    assert len(_tells(store, "ask_waiting")) == 2


def test_a_buying_side_escalation_is_not_a_listing_waiting(store: Store) -> None:
    want = store.create_want(query="road bike")
    store.create_thread(
        thread_id="fb:b1",
        side="buy",
        market="fb",
        counterpart_handle="sam",
        want_id=want["want_id"],
    )
    now = 1_000_000.0
    _escalate(store, "fb:b1", opened=now - 13 * HOUR)

    assert store.write_slow_tells(now=now) == 0


def test_an_unconfirmed_send_is_not_a_buyer_waiting_on_the_seller(store: Store) -> None:
    """That escalation asks whether our own message landed; framing it as a buyer's question
    invites an answer sent again over a message that may already have arrived."""
    tid = _sell_thread(store, _live_item(store))
    store.record_inbound(tid, msg_id="m1", text="is it still available?", ts=10.0)
    now = 1_000_000.0
    esc = store.escalate(tid, open_question="Is my message there?", kind="unconfirmed_send")
    with store._db.transaction() as conn:  # noqa: SLF001
        conn.execute("UPDATE escalations SET created_ts = ? WHERE id = ?", (now - DAY, esc["id"]))

    assert store.write_slow_tells(now=now) == 0
    assert _tells(store, "ask_waiting") == []


def test_a_held_thread_is_not_a_buyer_waiting_on_the_seller(store: Store) -> None:
    """A held thread is a suspected scam: calling its sender a waiting buyer, quoting their bait
    and asking the seller to reply is the engagement the hold exists to stop."""
    tid = _sell_thread(store, _live_item(store))
    store.record_inbound(tid, msg_id="m1", text="pay via this link to receive money", ts=10.0)
    now = 1_000_000.0
    store.hold_thread(tid, "payment link outside the marketplace")
    esc = store.escalate(tid, open_question="Looks like a scam. Block them?", kind="scam")
    with store._db.transaction() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE escalations SET created_ts = ? WHERE id = ?", (now - 13 * HOUR, esc["id"])
        )

    assert store.write_slow_tells(now=now) == 0
    assert _tells(store, "ask_waiting") == []

    store.release_thread(tid)  # a false alarm is a buyer waiting like any other
    assert store.write_slow_tells(now=now + MINUTE) == 1


def test_a_waiting_tell_on_a_thread_held_since_is_retired(store: Store) -> None:
    tid = _sell_thread(store, _live_item(store))
    now = 1_000_000.0
    _escalate(store, tid, opened=now - 13 * HOUR)
    store.write_slow_tells(now=now)
    store.hold_thread(tid, "payment link outside the marketplace")

    assert store.retire_moot_tells(now=now + MINUTE) == 1
    assert store.untold_tells() == []


def test_a_waiting_tell_answered_before_it_went_is_retired(store: Store) -> None:
    tid = _sell_thread(store, _live_item(store))
    now = 1_000_000.0
    esc = _escalate(store, tid, opened=now - 13 * HOUR)
    store.write_slow_tells(now=now)
    store.resolve_escalation(esc, "answered")

    assert store.retire_moot_tells(now=now + MINUTE) == 1
    assert store.untold_tells() == []


# --- a listing nobody wrote about ------------------------------------------------------------


def test_a_listing_live_a_week_with_no_buyer_is_told_once_a_week(store: Store) -> None:
    item = _live_item(store)
    now = 1_000_000.0
    _live_since(store, item, now - 7 * DAY)

    assert store.write_slow_tells(now=now) == 1
    assert store.write_slow_tells(now=now + 6 * DAY) == 0
    assert store.write_slow_tells(now=now + 7 * DAY + MINUTE) == 1

    first = _tells(store, "listing_quiet")[0]
    assert first["decision"] == 0
    assert first["thread_id"] is None
    assert first["facts"] == {"live_since_ts": now - 7 * DAY, "item_title": "IKEA desk"}


def test_a_listing_live_under_a_week_is_not_quiet_yet(store: Store) -> None:
    item = _live_item(store)
    now = 1_000_000.0
    _live_since(store, item, now - 6 * DAY)

    assert store.write_slow_tells(now=now) == 0


def test_a_listing_a_buyer_wrote_about_is_not_quiet(store: Store) -> None:
    item = _live_item(store)
    now = 1_000_000.0
    _live_since(store, item, now - 8 * DAY)
    _sell_thread(store, item)

    assert store.write_slow_tells(now=now) == 0


def test_a_quiet_tell_a_buyer_answered_before_it_went_is_retired(store: Store) -> None:
    item = _live_item(store)
    now = 1_000_000.0
    _live_since(store, item, now - 8 * DAY)
    store.write_slow_tells(now=now)
    _sell_thread(store, item)

    assert store.retire_moot_tells(now=now + MINUTE) == 1


def test_a_listing_goes_live_with_its_first_url_and_stops_with_its_last(store: Store) -> None:
    item = store.create_item(title="IKEA desk", list_price=100.0, currency="USD")
    assert store._db.query("SELECT live_since_ts FROM items")[0][0] is None  # noqa: SLF001

    store.record_listing_url(item["id"], "fb", "https://www.facebook.com/marketplace/item/1")
    first = store._db.query("SELECT live_since_ts FROM items")[0][0]  # noqa: SLF001
    store.record_listing_url(item["id"], "craigslist", "https://sfbay.craigslist.org/x/1.html")
    assert first is not None
    assert store._db.query("SELECT live_since_ts FROM items")[0][0] == first  # noqa: SLF001

    store.archive_listing_url(item["id"], "fb")
    assert store._db.query("SELECT live_since_ts FROM items")[0][0] == first  # noqa: SLF001
    store.archive_listing_url(item["id"], "craigslist")
    assert store._db.query("SELECT live_since_ts FROM items")[0][0] is None  # noqa: SLF001


# --- a sale the seller is closing themselves -------------------------------------------------


def test_resolving_a_close_as_the_sellers_own_is_recorded(make_ctx, store: Store) -> None:
    tid = _sell_thread(store, _live_item(store))

    _hand_over(make_ctx, store, tid, at=1_000_000.0)

    (deal,) = store._db.query("SELECT * FROM seller_handled_deals")  # noqa: SLF001
    assert deal["thread_id"] == tid and deal["settled_ts"] is None
    (spec,) = [s for s in all_specs() if s.name == "resolve_escalation"]
    schema = spec.input_schema
    assert schema["properties"]["seller_handles_it"]["type"] == "boolean"


def test_only_a_sale_on_a_listing_can_be_handed_to_the_seller(make_ctx, store: Store) -> None:
    want = store.create_want(query="road bike")
    store.create_thread(
        thread_id="fb:b1",
        side="buy",
        market="fb",
        counterpart_handle="sam",
        want_id=want["want_id"],
    )
    esc = store.escalate("fb:b1", open_question="Buy it?")

    with pytest.raises(ToolError):
        dispatch(
            "resolve_escalation",
            {"escalation_id": esc["id"], "resolution": "x", "seller_handles_it": True},
            make_ctx(TIER_ATTENDED),
        )
    assert store.get_escalation(esc["id"])["status"] == "open"


def test_a_deal_the_seller_is_closing_is_checked_on_once_a_day(make_ctx, store: Store) -> None:
    item = _live_item(store)
    tid = _sell_thread(store, item, handle="bob_k")
    handed = 1_000_000.0
    _hand_over(make_ctx, store, tid, at=handed)

    assert store.write_slow_tells(now=handed + 23 * HOUR) == 0
    assert store.write_slow_tells(now=handed + DAY) == 1
    store.mark_tells_told([t["id"] for t in _tells(store, "deal_checkin")], notice_id=1)
    assert store.write_slow_tells(now=handed + DAY + 23 * HOUR) == 0
    assert store.write_slow_tells(now=handed + 2 * DAY) == 1

    tell = _tells(store, "deal_checkin")[0]
    assert tell["decision"] == 1
    assert tell["facts"] == {"buyer_handle": "bob_k", "item_title": "IKEA desk"}


def test_an_untold_check_in_is_not_joined_by_another(make_ctx, store: Store) -> None:
    """The seller has not seen yesterday's question yet; a second copy would only repeat it."""
    tid = _sell_thread(store, _live_item(store))
    handed = 1_000_000.0
    _hand_over(make_ctx, store, tid, at=handed)
    store.write_slow_tells(now=handed + DAY)

    assert store.write_slow_tells(now=handed + 2 * DAY + MINUTE) == 0


def test_a_deal_on_a_listing_taken_down_is_not_checked_on(make_ctx, store: Store) -> None:
    item = _live_item(store)
    tid = _sell_thread(store, item)
    handed = 1_000_000.0
    _hand_over(make_ctx, store, tid, at=handed)
    store.archive_listing_url(item, "fb")

    assert store.write_slow_tells(now=handed + DAY) == 0


def test_a_deal_on_a_listing_taken_down_stays_settled_after_a_relist(
    make_ctx, store: Store
) -> None:
    """A relist is a new sale; the buyer of the old one is not checked on again."""
    item = _live_item(store)
    tid = _sell_thread(store, item)
    handed = 1_000_000.0
    _hand_over(make_ctx, store, tid, at=handed)
    store.archive_listing_url(item, "fb")
    store.record_listing_url(item, "fb", "https://www.facebook.com/marketplace/item/relisted")

    (deal,) = store._db.query("SELECT settled_ts FROM seller_handled_deals")  # noqa: SLF001
    assert deal["settled_ts"] is not None
    assert store.write_slow_tells(now=handed + 3 * DAY) == 0


@pytest.mark.parametrize("settle", ["sold", "released", "withdrew"])
def test_a_deal_that_sold_or_fell_through_is_not_checked_on_again(
    make_ctx, store: Store, settle: str
) -> None:
    item = _live_item(store)
    tid = _sell_thread(store, item)
    store.record_inbound(tid, msg_id="m1", text="i'll take it", ts=10.0)
    store.negotiate_offer(item, tid, "bob_k", 100.0, config=Config())
    handed = 1_000_000.0
    _hand_over(make_ctx, store, tid, at=handed)
    store.write_slow_tells(now=handed + DAY)

    if settle == "sold":
        store.negotiate_confirm_sold(item, tid)
    elif settle == "released":
        store.negotiate_release(item)
    else:
        store.negotiate_withdraw(item, tid, msg_id="m1", notice=lambda _r: ("gone", None))

    assert store.retire_moot_tells(now=handed + DAY + MINUTE) == 1
    assert store.write_slow_tells(now=handed + 3 * DAY) == 0


def test_a_checkout_minted_after_the_hand_over_ends_the_check_ins(make_ctx, store: Store) -> None:
    """The seller changed their mind and Sellee is closing the sale again: asking whether they
    closed it is false, and its "fell through" would reopen an item the buyer can still pay for."""
    item = _live_item(store)
    tid = _sell_thread(store, item)
    handed = 1_000_000.0
    _hand_over(make_ctx, store, tid, at=handed)
    store.write_slow_tells(now=handed + DAY)

    store.record_checkout(
        sale_id="sale-1",
        item_id=item,
        thread_id=tid,
        checkout_url="https://carousell.ai/checkout/sale-1",
        price=90.0,
        currency="USD",
    )

    assert store.retire_moot_tells(now=handed + DAY + MINUTE) == 1
    assert store.write_slow_tells(now=handed + 2 * DAY + HOUR) == 0
    assert len(_tells(store, "deal_checkin")) == 1


def test_fell_through_on_one_of_two_sales_settles_only_that_one(make_ctx, store: Store) -> None:
    item = _live_item(store)
    first = _sell_thread(store, item, "fb:1")
    second = _sell_thread(store, item, "fb:2")
    handed = 1_000_000.0
    _hand_over(make_ctx, store, first, at=handed)
    _hand_over(make_ctx, store, second, at=handed)

    with pytest.raises(ToolError, match="closing 2 sales"):
        dispatch("negotiate_release", {"item_id": item}, make_ctx(TIER_ATTENDED))
    dispatch("negotiate_release", {"item_id": item, "thread_id": first}, make_ctx(TIER_ATTENDED))

    assert store.seller_handled_threads(item) == [second]
    assert store.write_slow_tells(now=handed + DAY) == 1
    assert [t["thread_id"] for t in _tells(store, "deal_checkin")] == [second]


def test_fell_through_on_the_one_sale_taken_over_settles_it(make_ctx, store: Store) -> None:
    item = _live_item(store)
    _hand_over(make_ctx, store, _sell_thread(store, item), at=1_000_000.0)

    dispatch("negotiate_release", {"item_id": item}, make_ctx(TIER_ATTENDED))

    assert store.seller_handled_threads(item) == []


def test_release_with_no_sale_taken_over_still_reopens_the_item(make_ctx, store: Store) -> None:
    item = _live_item(store)

    result = dispatch("negotiate_release", {"item_id": item}, make_ctx(TIER_ATTENDED))

    assert result["item_state"] == store.negotiate_status(item)["item_state"]


def _checkin_notice(make_ctx, seller: Store, item: str, *, handed: float) -> int:
    """Hand the sale on fb:1 over and let the lane send its check-in; the check-in's notice."""
    _hand_over(make_ctx, seller, "fb:1", at=handed)
    activity.updates_lane(store=seller, now=handed + DAY)
    activity.updates_lane(store=seller, now=handed + DAY + activity.HOLD_SEC)
    (notice,) = _notices(seller, "update:")
    assert "bob_k" in notice["text"]
    return notice["id"]


def test_a_tapped_check_in_answers_for_the_buyer_it_named(make_ctx, seller: Store) -> None:
    """Another sale on the listing was taken over too; the tap names its own."""
    item = _live_item(seller)
    _sell_thread(seller, item, "fb:1", handle="bob_k")
    other = _sell_thread(seller, item, "fb:2", handle="sam")
    seller.negotiate_offer(item, other, "sam", 95.0, config=Config())
    handed = 1_000_000.0
    notice_id = _checkin_notice(make_ctx, seller, item, handed=handed)
    _hand_over(make_ctx, seller, other, at=handed + DAY + activity.HOLD_SEC + MINUTE)

    result = dispatch(
        "negotiate_confirm_sold", {"item_id": item, "notice_id": notice_id}, make_ctx(TIER_ATTENDED)
    )

    assert result["close_threads"] == [other]


def test_a_tap_from_an_old_check_in_never_sells_to_a_buyer_it_did_not_name(
    make_ctx, seller: Store
) -> None:
    """That sale fell through and another buyer was taken over since: the old Sold button is
    refused rather than marking the new buyer sold."""
    item = _live_item(seller)
    _sell_thread(seller, item, "fb:1", handle="bob_k")
    later = _sell_thread(seller, item, "fb:2", handle="sam")
    handed = 1_000_000.0
    notice_id = _checkin_notice(make_ctx, seller, item, handed=handed)
    dispatch("negotiate_release", {"item_id": item}, make_ctx(TIER_ATTENDED))
    _hand_over(make_ctx, seller, later, at=handed + 2 * DAY)

    for tool in ("negotiate_confirm_sold", "negotiate_release"):
        with pytest.raises(ToolError, match="no longer one the seller is closing"):
            dispatch(tool, {"item_id": item, "notice_id": notice_id}, make_ctx(TIER_ATTENDED))
    assert seller.negotiate_status(item)["item_state"] != "sold"
    assert seller.seller_handled_threads(item) == [later]


def test_a_notice_that_asked_about_no_sale_is_refused(make_ctx, store: Store) -> None:
    item = _live_item(store)
    _hand_over(make_ctx, store, _sell_thread(store, item), at=1_000_000.0)

    with pytest.raises(ToolError, match="asked about no sale"):
        dispatch(
            "negotiate_confirm_sold", {"item_id": item, "notice_id": 999}, make_ctx(TIER_ATTENDED)
        )


# --- the lane --------------------------------------------------------------------------------


def test_the_lane_writes_a_check_in_and_sends_it_with_its_answers(make_ctx, seller: Store) -> None:
    tid = _sell_thread(seller, _live_item(seller))
    handed = 1_000_000.0
    _hand_over(make_ctx, seller, tid, at=handed)

    activity.updates_lane(store=seller, now=handed + DAY)
    assert _notices(seller, "update:") == []  # just written: it waits out the hold like any tell
    activity.updates_lane(store=seller, now=handed + DAY + activity.HOLD_SEC)

    (notice,) = _notices(seller, "update:")
    assert "still on it with bob_k?" in notice["text"]
    labels = [label for label, _token in json.loads(notice["controls"])]
    assert labels == list(CHECKIN_OPTIONS)


def test_a_check_in_is_sent_even_with_activity_updates_off(make_ctx, seller: Store) -> None:
    """The seller asked for it by taking the sale over, and was promised it."""
    seed_setting(seller, "activity_updates", "off")
    tid = _sell_thread(seller, _live_item(seller))
    handed = 1_000_000.0
    _hand_over(make_ctx, seller, tid, at=handed)

    activity.updates_lane(store=seller, now=handed + DAY)
    activity.updates_lane(store=seller, now=handed + DAY + activity.HOLD_SEC)

    (notice,) = _notices(seller, "update:")
    assert "still on it with bob_k?" in notice["text"]


def test_activity_updates_off_still_keeps_quiet_listings_back(seller: Store) -> None:
    seed_setting(seller, "activity_updates", "off")
    item = _live_item(seller)
    now = 1_000_000.0
    _live_since(seller, item, now - 8 * DAY)

    activity.updates_lane(store=seller, now=now)
    activity.updates_lane(store=seller, now=now + activity.HOLD_SEC)

    assert _notices(seller, "update") == []


def test_sold_on_a_check_in_sells_to_the_buyer_the_seller_took_over(make_ctx, store: Store) -> None:
    """The tap names the sale only in words; the thread comes from the hand-over record, so a
    second buyer on the same listing is never the one marked as sold to."""
    item = _live_item(store)
    tid = _sell_thread(store, item, "fb:1", handle="bob_k")
    other = _sell_thread(store, item, "fb:2", handle="bob_k")
    store.negotiate_offer(item, other, "bob_k", 95.0, config=Config())
    _hand_over(make_ctx, store, tid, at=1_000_000.0)

    result = dispatch("negotiate_confirm_sold", {"item_id": item}, make_ctx(TIER_ATTENDED))

    # the buyer the seller took over is the one sold to; the other is the one closed
    assert result["close_threads"] == [other]
    assert store.negotiate_status(item)["item_state"] == "sold"


def test_sold_with_no_sale_taken_over_must_name_the_buyer(make_ctx, store: Store) -> None:
    item = _live_item(store)
    _sell_thread(store, item)

    with pytest.raises(ToolError, match="name the thread_id"):
        dispatch("negotiate_confirm_sold", {"item_id": item}, make_ctx(TIER_ATTENDED))
    assert store.negotiate_status(item)["item_state"] != "sold"


def test_sold_with_two_sales_taken_over_must_name_the_buyer(make_ctx, store: Store) -> None:
    item = _live_item(store)
    _hand_over(make_ctx, store, _sell_thread(store, item, "fb:1"), at=1_000_000.0)
    _hand_over(make_ctx, store, _sell_thread(store, item, "fb:2"), at=1_000_000.0)

    with pytest.raises(ToolError, match="closing 2 sales"):
        dispatch("negotiate_confirm_sold", {"item_id": item}, make_ctx(TIER_ATTENDED))


def test_the_lane_writes_while_unbound_so_a_catchup_can_tell_it(store: Store) -> None:
    item = _live_item(store)
    now = 1_000_000.0
    _live_since(store, item, now - 8 * DAY)

    activity.updates_lane(store=store, now=now)

    assert len(_tells(store, "listing_quiet")) == 1
    assert _notices(store, "update") == []


def test_a_paused_lane_writes_nothing(seller: Store) -> None:
    item = _live_item(seller)
    now = 1_000_000.0
    _live_since(seller, item, now - 8 * DAY)
    seller.set_paused(True, source="test")

    activity.updates_lane(store=seller, now=now)

    assert _tells(seller, "listing_quiet") == []


# --- the weekly line --------------------------------------------------------------------------


WEEKLY_THREAD = "fb:1"


def _weekly_seller(store: Store, zone: str = SATURDAY_ZONE) -> str:
    """A seller on their Saturday with a listing live 8 days that a buyer wrote about once, the
    week before: a quiet week, and no quiet-listing tell to send instead."""
    store.set_seller_config_section("basics", {"region": "US", "currency": "USD", "timezone": zone})
    item = _live_item(store, "IKEA desk")
    _live_since(store, item, SATURDAY_UTC_MORNING - 8 * DAY)
    _sell_thread(store, item, WEEKLY_THREAD)
    store.record_inbound(
        WEEKLY_THREAD, msg_id="old", text="still there?", ts=SATURDAY_UTC_MORNING - 9 * DAY
    )
    return item


def _answered_tell(store: Store, item: str, *, at: float) -> None:
    facts = {
        "buyer_said": "is it still there?",
        "we_said": "yes, still available!",
        "first_on_item": False,
        "item_title": "IKEA desk",
    }
    with store._db.transaction() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO tells (item_id, thread_id, kind, decision, facts, created_ts) "
            "VALUES (?, ?, 'buyer_answered', 0, ?, ?)",
            (item, WEEKLY_THREAD, json.dumps(facts), at),
        )


def _run_lane(store: Store, start: float, minutes: int) -> None:
    for minute in range(minutes + 1):
        activity.updates_lane(store=store, now=start + minute * MINUTE)


def test_the_weekly_line_goes_on_the_sellers_saturday(seller: Store) -> None:
    _weekly_seller(seller)

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    (notice,) = _notices(seller, "update-weekly:")
    assert notice["ref"] == "update-weekly:2026-W45"
    assert notice["holdable"] == 1
    assert notice["text"] == (
        "👋 Still on it: 1 listing live (“IKEA desk”). No buyers wrote this week. "
        "Want me to lower it? Tell me the new price."
    )
    assert leak_paths(notice["text"], FLOOR) == []


@pytest.mark.parametrize("live_for", [8 * DAY, 7 * DAY - 5 * MINUTE])
def test_a_quiet_listing_folds_into_the_weekly_line(seller: Store, live_for: float) -> None:
    """A listing turning a week old with no buyer is told by the weekly line, not by a text of its
    own beside it, and is stamped told by it so no update repeats it."""
    seller.set_seller_config_section(
        "basics", {"region": "US", "currency": "USD", "timezone": SATURDAY_ZONE}
    )
    _live_since(seller, _live_item(seller, "IKEA desk"), SATURDAY_UTC_MORNING - live_for)

    _run_lane(seller, SATURDAY_UTC_MORNING, 30)

    (notice,) = _notices(seller, "update")
    assert notice["ref"] == "update-weekly:2026-W45"
    assert notice["text"] == (
        "👋 Still on it: 1 listing live (“IKEA desk”). No buyers wrote this week. "
        "Want me to lower it? Tell me the new price."
    )
    (tell,) = _tells(seller, "listing_quiet")
    assert tell["notice_id"] == notice["id"]


def test_a_quiet_listing_alone_sends_nothing_on_a_weekday(seller: Store) -> None:
    """A text that arrives only because a listing turned a week old reads as a timer, not news."""
    seller.set_seller_config_section(
        "basics", {"region": "US", "currency": "USD", "timezone": FRIDAY_ZONE}
    )
    _live_since(seller, _live_item(seller), SATURDAY_UTC_MORNING - 8 * DAY)

    _run_lane(seller, SATURDAY_UTC_MORNING, int(activity.MAX_HOLD_SEC // MINUTE) + 5)

    assert _notices(seller, "update") == []
    (tell,) = _tells(seller, "listing_quiet")
    assert tell["told_ts"] is None


def test_a_quiet_listing_rides_an_update_something_else_made_due(seller: Store) -> None:
    seller.set_seller_config_section(
        "basics", {"region": "US", "currency": "USD", "timezone": FRIDAY_ZONE}
    )
    lamp = _live_item(seller, "Arc floor lamp")
    _live_since(seller, lamp, SATURDAY_UTC_MORNING - 8 * DAY)
    desk = _live_item(seller, "IKEA desk")
    _sell_thread(seller, desk, WEEKLY_THREAD)
    _answered_tell(seller, desk, at=SATURDAY_UTC_MORNING)

    _run_lane(seller, SATURDAY_UTC_MORNING, int(activity.HOLD_SEC // MINUTE))

    (notice,) = _notices(seller, "update")
    assert notice["ref"].startswith("update:")
    assert "🗣 IKEA desk" in notice["text"] and "💤 Arc floor lamp" in notice["text"]
    assert all(t["notice_id"] == notice["id"] for t in _tells(seller, "listing_quiet"))


def test_the_weekly_line_waits_for_an_update_in_its_hold(seller: Store) -> None:
    """A buyer answered five minutes ago: that update goes when its hold ends, and the weekly line
    does not go ahead of it."""
    item = _weekly_seller(seller)
    _answered_tell(seller, item, at=SATURDAY_UTC_MORNING - 5 * MINUTE)

    _run_lane(seller, SATURDAY_UTC_MORNING, 20)

    (notice,) = _notices(seller, "update")
    assert notice["ref"].startswith("update:")


def test_no_weekly_line_for_a_seller_whose_listings_just_went_up(seller: Store) -> None:
    """Never told anything, but nothing has been live a week: not a quiet week yet."""
    seller.set_seller_config_section(
        "basics", {"region": "US", "currency": "USD", "timezone": SATURDAY_ZONE}
    )
    _live_since(seller, _live_item(seller), SATURDAY_UTC_MORNING - DAY)

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    assert _notices(seller, "update") == []


def test_the_weekly_line_offers_to_lower_only_what_has_been_live_a_week(seller: Store) -> None:
    _weekly_seller(seller)
    lamp = _live_item(seller, "Arc floor lamp")
    _live_since(seller, lamp, SATURDAY_UTC_MORNING - 2 * DAY)

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    (notice,) = _notices(seller, "update-weekly:")
    assert notice["text"].endswith("Want me to lower “IKEA desk”? Tell me the new price.")


def test_the_weekly_line_goes_once_a_week(seller: Store) -> None:
    _weekly_seller(seller)

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)
    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING + 5 * HOUR)

    assert len(_notices(seller, "update-weekly:")) == 1


def test_no_weekly_line_while_it_is_still_friday_where_the_seller_is(seller: Store) -> None:
    _weekly_seller(seller, zone=FRIDAY_ZONE)

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    assert _notices(seller, "update-weekly:") == []


def test_no_weekly_line_after_an_update_this_week(seller: Store) -> None:
    item = _weekly_seller(seller)
    with seller._db.transaction() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO tells (item_id, kind, decision, facts, created_ts, told_ts, notice_id) "
            "VALUES (?, 'listing_quiet', 0, '{}', ?, ?, 1)",
            (item, SATURDAY_UTC_MORNING - 6 * DAY, SATURDAY_UTC_MORNING - 6 * DAY),
        )

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    assert _notices(seller, "update-weekly:") == []


def test_no_weekly_line_with_nothing_live(seller: Store) -> None:
    seller.set_seller_config_section(
        "basics", {"region": "US", "currency": "USD", "timezone": SATURDAY_ZONE}
    )

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    assert _notices(seller, "update-weekly:") == []


@pytest.mark.parametrize("mode", ["decisions", "off"])
def test_no_weekly_line_unless_the_seller_wants_every_update(seller: Store, mode: str) -> None:
    _weekly_seller(seller)
    seed_setting(seller, "activity_updates", mode)

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    assert _notices(seller, "update-weekly:") == []


def test_the_weekly_line_counts_the_buyers_who_wrote_this_week(seller: Store) -> None:
    _weekly_seller(seller)
    seller.record_inbound(
        WEEKLY_THREAD, msg_id="m1", text="still there?", ts=SATURDAY_UTC_MORNING - DAY
    )

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    (notice,) = _notices(seller, "update-weekly:")
    assert "1 buyer wrote this week." in notice["text"]


def test_the_weekly_line_counts_only_buyers_of_what_is_still_live(seller: Store) -> None:
    """A buyer of a listing since taken down would read as someone writing about what is up."""
    _weekly_seller(seller)
    gone = _live_item(seller, "Arc floor lamp")
    _sell_thread(seller, gone, "fb:9", handle="sam")
    seller.record_inbound("fb:9", msg_id="m9", text="still there?", ts=SATURDAY_UTC_MORNING - DAY)
    seller.archive_listing_url(gone, "fb")

    activity.updates_lane(store=seller, now=SATURDAY_UTC_MORNING)

    (notice,) = _notices(seller, "update-weekly:")
    assert "No buyers wrote this week." in notice["text"]


# --- what the channel pass is told ------------------------------------------------------------


def test_the_check_in_is_no_longer_promised_as_unavailable() -> None:
    text = skills.load("seller-comms")
    assert "Not available yet" not in text
    assert "seller_handles_it" in text


def test_the_check_in_rule_lives_in_the_tool_that_sets_it() -> None:
    """Said once, where the flag is set; the skill only says what to tell the seller."""
    text = skills.load("seller-comms")
    (spec,) = [s for s in all_specs() if s.name == "resolve_escalation"]
    flag = spec.input_schema["properties"]["seller_handles_it"]["description"]
    assert "once a day" in flag and "activity_updates" in flag
    taps = text[text.index("**Sale completion / fell through.**") : text.index("**Anomaly.**")]
    for cadence in ("daily", "once a day", "tomorrow", "every day"):
        assert cadence not in taps


def test_a_live_listing_price_change_is_left_to_its_own_skill() -> None:
    text = skills.load("seller-comms")
    taps = text[text.index("**Activity updates**") : text.index("**Scam confirmation.**")]
    assert "update_live_listing" not in taps
