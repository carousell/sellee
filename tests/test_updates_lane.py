"""The activity-update lane: when an update goes to the seller, what it carries, and that the tells
it carries are stamped told by it in the same transaction that queues it.

The tells are written straight into the ledger with the ages each case needs, on listings that are
live; the composer's own rules are covered by its golden tests, so these check only the gates
around it: pause, an unbound channel, quiet hours, the hold and its cap, the seller's setting, the
once-guard, and the catchups that tell what the lane could not.
"""

from __future__ import annotations

import json
from datetime import datetime
from time import time

import pytest
from tests.conftest import leak_paths, seed_setting

from sellee import skills
from sellee.channel import activity, fastpaths, outbound
from sellee.db import Database
from sellee.store import Store
from sellee.store import updates as updates_store
from sellee.tools import TIER_ATTENDED
from sellee.tools.registry import all_specs, dispatch
from sellee.updates import DECISION_OPTIONS, MAX_LINES_PER_SECTION

MINUTE = 60.0
HOUR = 3600.0
# A weekday afternoon in the daemon's own zone, which is the clock quiet hours are read on.
NOW = datetime(2026, 11, 4, 15, 0).timestamp()
FLOOR = 73.0


def _bind(store: Store) -> None:
    store.arm_bind("sellee_bot", "n1")
    store.complete_bind(555, update_offset=1, nonce=store.get_channel()["bind_nonce"])


def _live_item(store: Store, title: str) -> str:
    item = store.create_item(title=title, list_price=100.0, currency="USD")
    store.set_floor(item["id"], FLOOR, "seller")
    store.record_listing_url(
        item["id"], "fb", f"https://www.facebook.com/marketplace/item/{item['id']}"
    )
    return item["id"]


def _tell(store: Store, item_id: str, kind: str, *, age: float, thread: str, now=NOW) -> int:
    if kind == "offer_declined":
        facts: dict = {
            "buyer_handle": "dana",
            "amount": 40.0,
            "currency": "USD",
            "item_title": "KitchenAid mixer",
        }
    else:
        facts = {
            "buyer_handle": "bob",
            "buyer_said": "is it still available?",
            "we_said": "Yes, it is.",
            "first_on_item": False,
            "item_title": "IKEA desk",
        }
    with store._db.transaction() as conn:  # noqa: SLF001 — tests arrange ledger rows directly
        cur = conn.execute(
            "INSERT INTO tells (item_id, thread_id, kind, decision, facts, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                item_id,
                thread,
                kind,
                1 if kind == "offer_declined" else 0,
                json.dumps(facts),
                now - age,
            ),
        )
        return int(cur.lastrowid)


def _tell_rows(store: Store) -> dict:
    rows = store._db.query("SELECT id, told_ts, notice_id FROM tells ORDER BY id")  # noqa: SLF001
    return {r["id"]: (r["told_ts"], r["notice_id"]) for r in rows}


def _update_notices(store: Store) -> list:
    rows = store._db.query(  # noqa: SLF001
        "SELECT * FROM notices WHERE ref LIKE 'update:%' ORDER BY id"
    )
    return [dict(r) for r in rows]


@pytest.fixture
def seller(store: Store) -> Store:
    _bind(store)
    return store


# --- what goes out, and that the tells it carries are stamped with it ---------------------------


def test_a_held_tell_goes_as_one_notice_and_is_stamped_told_by_it(seller: Store, bus) -> None:
    mixer, desk = _live_item(seller, "KitchenAid mixer"), _live_item(seller, "IKEA desk")
    offer = _tell(seller, mixer, "offer_declined", age=20 * MINUTE, thread="fb:o1")
    answered = _tell(seller, desk, "buyer_answered", age=16 * MINUTE, thread="fb:a1")

    activity.updates_lane(store=seller, bus=bus, now=NOW)

    (notice,) = _update_notices(seller)
    assert notice["ref"] == f"update:{offer}"
    assert notice["holdable"] == 1  # a proactive push waits out quiet hours
    assert "under your minimum" in notice["text"]
    assert "answered a buyer" in notice["text"]
    labels = [label for label, _token in json.loads(notice["controls"])]
    assert labels == list(DECISION_OPTIONS)  # the decision rides as tappable options
    assert _tell_rows(seller) == {offer: (NOW, notice["id"]), answered: (NOW, notice["id"])}
    assert leak_paths(notice["text"], FLOOR) == []


def test_an_update_with_no_decision_in_it_has_no_buttons(seller: Store) -> None:
    desk = _live_item(seller, "IKEA desk")
    _tell(seller, desk, "buyer_answered", age=20 * MINUTE, thread="fb:a1")

    activity.updates_lane(store=seller, now=NOW)

    (notice,) = _update_notices(seller)
    assert notice["controls"] is None


def test_the_lane_records_what_it_queued(seller: Store, bus) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    offer = _tell(seller, mixer, "offer_declined", age=20 * MINUTE, thread="fb:o1")

    activity.updates_lane(store=seller, bus=bus, now=NOW)

    (event,) = bus.store.read(kinds=[activity.UPDATE_QUEUED_EVENT])
    (notice,) = _update_notices(seller)
    assert event.kind == "activity_update.queued"  # apart from the installer's update.* events
    assert event.payload["notice_id"] == notice["id"]
    assert event.payload["ref"] == f"update:{offer}"
    assert event.payload["tells"] == 1
    assert event.payload["asks"] is True
    assert leak_paths(event.payload, FLOOR) == []


# --- the hold, and its cap --------------------------------------------------------------------


def test_a_tell_inside_the_hold_waits_for_whatever_lands_next(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=activity.HOLD_SEC - MINUTE, thread="fb:o1")

    activity.updates_lane(store=seller, now=NOW)

    assert _update_notices(seller) == []
    assert all(told is None for told, _ in _tell_rows(seller).values())


def test_a_tell_goes_the_moment_its_hold_is_up(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=activity.HOLD_SEC - 1, thread="fb:o1")

    activity.updates_lane(store=seller, now=NOW)
    assert _update_notices(seller) == []  # a second short

    activity.updates_lane(store=seller, now=NOW + 1)
    assert len(_update_notices(seller)) == 1


def test_a_stream_of_tells_holds_until_the_oldest_reaches_the_cap(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=activity.MAX_HOLD_SEC - MINUTE, thread="fb:o1")
    _tell(seller, mixer, "offer_declined", age=MINUTE, thread="fb:o2")

    activity.updates_lane(store=seller, now=NOW)

    assert _update_notices(seller) == []


@pytest.mark.parametrize("mode", ["all", "decisions"])
def test_a_buyer_countering_inside_every_hold_still_reaches_the_seller_at_the_cap(
    seller: Store, monkeypatch, mode: str
) -> None:
    """Each counter under the minimum replaces the buyer's untold offer with a fresh one, so their
    own tell is never older than the gap between counters. The cap runs from their first, or a
    buyer countering every 14 minutes would keep the seller from hearing for as long as they kept
    at it."""
    seed_setting(seller, "activity_updates", mode)
    mixer = _live_item(seller, "KitchenAid mixer")
    start = NOW - 2 * HOUR
    clock = {"now": start}
    monkeypatch.setattr(updates_store, "_now", lambda: clock["now"])

    queued_at = None
    for minute in range(int(2 * HOUR / MINUTE)):
        clock["now"] = start + minute * MINUTE
        if minute % 14 == 0:
            with seller._db.transaction() as conn:  # noqa: SLF001
                updates_store.record_offer_decision_in_txn(
                    conn,
                    item_id=mixer,
                    thread_id="fb:o1",
                    handle="dana",
                    offer=40.0 + minute / 14,
                    currency="USD",
                    result={"decision": "decline"},
                    floor=FLOOR,
                )
        activity.updates_lane(store=seller, now=clock["now"])
        if _update_notices(seller):
            queued_at = clock["now"]
            break

    assert queued_at == start + activity.MAX_HOLD_SEC
    (latest,) = (t for t in _tell_rows(seller).items() if t[1][1] is not None)
    assert latest[0] == max(_tell_rows(seller))  # only the offer still standing is told


def test_what_an_update_had_no_room_for_waits_a_hold_for_the_next(seller: Store) -> None:
    """The composer names a few listings that need the seller and counts the rest; the rest are
    still untold and long past their own hold, so only the hold from the last update keeps them
    from following a minute later."""
    listings = MAX_LINES_PER_SECTION + 1
    for n in range(listings):
        item = _live_item(seller, f"Lamp {n}")
        _tell(seller, item, "offer_declined", age=20 * MINUTE, thread=f"fb:o{n}")

    activity.updates_lane(store=seller, now=NOW)
    (first,) = _update_notices(seller)
    assert "+1 more" in first["text"]
    left = [i for i, (told, _) in _tell_rows(seller).items() if told is None]
    assert len(left) == 1

    activity.updates_lane(store=seller, now=NOW + MINUTE)
    activity.updates_lane(store=seller, now=NOW + activity.HOLD_SEC - 1)
    assert len(_update_notices(seller)) == 1

    activity.updates_lane(store=seller, now=NOW + activity.HOLD_SEC)
    assert len(_update_notices(seller)) == 2
    assert _tell_rows(seller)[left[0]][0] == NOW + activity.HOLD_SEC


def test_a_stream_of_tells_still_goes_once_the_oldest_reaches_the_cap(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    oldest = _tell(seller, mixer, "offer_declined", age=activity.MAX_HOLD_SEC, thread="fb:o1")
    _tell(seller, mixer, "offer_declined", age=MINUTE, thread="fb:o2")

    activity.updates_lane(store=seller, now=NOW)

    (notice,) = _update_notices(seller)
    assert notice["ref"] == f"update:{oldest}"
    assert "2 offers" in notice["text"]


def test_tells_on_one_hold_go_together_not_one_by_one(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=30 * MINUTE, thread="fb:o1")
    _tell(seller, mixer, "offer_declined", age=5 * MINUTE, thread="fb:o2")

    activity.updates_lane(store=seller, now=NOW)
    assert _update_notices(seller) == []  # the newest is still inside its hold

    activity.updates_lane(store=seller, now=NOW + activity.HOLD_SEC)
    (notice,) = _update_notices(seller)
    assert "2 offers" in notice["text"]


def test_a_tap_on_an_update_reads_as_the_seller_answering_it(seller: Store) -> None:
    """The update's options are an ask like any other: a tap resolves to its label and the update
    it answers, and goes to a channel pass from there."""
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=20 * MINUTE, thread="fb:o1")
    activity.updates_lane(store=seller, now=NOW)
    (notice,) = _update_notices(seller)

    tapped = seller.notice_option(notice["id"], f"n{notice['id']}:a1")

    assert tapped == {"label": "Hold", "text": notice["text"]}


# --- the gates ------------------------------------------------------------------------------


def test_nothing_goes_and_nothing_is_stamped_while_paused(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1")
    seller.set_paused(True, source="test")

    activity.updates_lane(store=seller, now=NOW)

    assert _update_notices(seller) == []
    assert all(told is None for told, _ in _tell_rows(seller).values())


def test_nothing_goes_while_no_channel_is_bound(store: Store) -> None:
    mixer = _live_item(store, "KitchenAid mixer")
    _tell(store, mixer, "offer_declined", age=HOUR, thread="fb:o1")

    activity.updates_lane(store=store, now=NOW)

    assert _update_notices(store) == []
    assert all(told is None for told, _ in _tell_rows(store).values())  # they wait for catchup


def test_a_night_of_tells_waits_out_quiet_hours_and_goes_as_one(seller: Store) -> None:
    seed_setting(seller, "quiet_hours", [2300, 800])
    night = datetime(2026, 11, 4, 23, 30).timestamp()
    morning = datetime(2026, 11, 5, 8, 1).timestamp()
    mixer, desk = _live_item(seller, "KitchenAid mixer"), _live_item(seller, "IKEA desk")
    _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1", now=night)
    _tell(seller, desk, "buyer_answered", age=-3 * HOUR, thread="fb:a1", now=night)

    for tick in (night, night + 3 * HOUR, night + 6 * HOUR):
        activity.updates_lane(store=seller, now=tick)
    assert _update_notices(seller) == []
    assert all(told is None for told, _ in _tell_rows(seller).values())

    activity.updates_lane(store=seller, now=morning)

    (notice,) = _update_notices(seller)
    assert "under your minimum" in notice["text"] and "answered a buyer" in notice["text"]
    assert all(told == morning for told, _ in _tell_rows(seller).values())


@pytest.mark.parametrize("minute", [44, 50, 59])  # the window 16, 10 and 1 minute off
def test_an_update_due_just_before_quiet_hours_goes_before_them(seller: Store, minute: int) -> None:
    """The drain delivers a queued update within seconds, so one due before the window opens
    reaches the seller that evening rather than waiting for the morning."""
    seed_setting(seller, "quiet_hours", [2300, 800])
    tick = datetime(2026, 11, 4, 22, minute).timestamp()
    mixer = _live_item(seller, "KitchenAid mixer")
    offer = _tell(seller, mixer, "offer_declined", age=20 * MINUTE, thread="fb:o1", now=tick)

    activity.updates_lane(store=seller, now=tick)

    (notice,) = _update_notices(seller)
    assert _tell_rows(seller) == {offer: (tick, notice["id"])}


def test_the_last_minutes_of_quiet_hours_still_hold(seller: Store) -> None:
    seed_setting(seller, "quiet_hours", [2300, 800])
    mixer = _live_item(seller, "KitchenAid mixer")
    tick = datetime(2026, 11, 5, 7, 59).timestamp()
    _tell(seller, mixer, "offer_declined", age=20 * MINUTE, thread="fb:o1", now=tick)

    activity.updates_lane(store=seller, now=tick)

    assert _update_notices(seller) == []


def test_quiet_hours_are_the_window_the_drain_already_holds_for(seller: Store) -> None:
    seed_setting(seller, "quiet_hours", [2300, 800])
    assert outbound.in_quiet_hours(seller, datetime(2026, 11, 4, 23, 30).timestamp())
    assert not outbound.in_quiet_hours(seller, NOW)


# --- the seller's setting ---------------------------------------------------------------------


def test_off_sends_nothing_and_leaves_the_tells_for_catchup(seller: Store) -> None:
    seed_setting(seller, "activity_updates", "off")
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1")

    activity.updates_lane(store=seller, now=NOW)

    assert _update_notices(seller) == []
    assert all(told is None for told, _ in _tell_rows(seller).values())


def test_off_still_retires_a_tell_once_it_is_stale(seller: Store) -> None:
    seed_setting(seller, "activity_updates", "off")
    mixer = _live_item(seller, "KitchenAid mixer")
    stale = _tell(seller, mixer, "offer_declined", age=49 * HOUR, thread="fb:o1")
    fresh = _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o2")

    activity.updates_lane(store=seller, now=NOW)

    assert _update_notices(seller) == []
    assert _tell_rows(seller) == {stale: (NOW, None), fresh: (None, None)}


def test_decisions_tells_only_what_needs_the_seller(seller: Store) -> None:
    seed_setting(seller, "activity_updates", "decisions")
    mixer, desk = _live_item(seller, "KitchenAid mixer"), _live_item(seller, "IKEA desk")
    offer = _tell(seller, mixer, "offer_declined", age=20 * MINUTE, thread="fb:o1")
    answered = _tell(seller, desk, "buyer_answered", age=HOUR, thread="fb:a1")

    activity.updates_lane(store=seller, now=NOW)

    (notice,) = _update_notices(seller)
    assert notice["ref"] == f"update:{offer}"  # the answered buyer is not what it is about
    assert "answered" not in notice["text"]
    rows = _tell_rows(seller)
    assert rows[offer] == (NOW, notice["id"])
    assert rows[answered] == (None, None)  # left for catchup, never sent


def test_decisions_with_nothing_to_decide_sends_nothing(seller: Store) -> None:
    seed_setting(seller, "activity_updates", "decisions")
    desk = _live_item(seller, "IKEA desk")
    _tell(seller, desk, "buyer_answered", age=HOUR, thread="fb:a1")

    activity.updates_lane(store=seller, now=NOW)

    assert _update_notices(seller) == []


# --- told once ------------------------------------------------------------------------------


def test_a_restart_never_tells_the_same_thing_twice(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1")
    activity.updates_lane(store=seller, now=NOW)

    restarted = Store(Database(seller._db.path))  # noqa: SLF001
    activity.updates_lane(store=restarted, now=NOW + MINUTE)
    activity.updates_lane(store=restarted, now=NOW + 2 * HOUR)

    assert len(_update_notices(restarted)) == 1


def test_an_update_already_queued_for_the_oldest_tell_is_not_queued_again(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    offer = _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1")
    seller.queue_notice("an earlier update", ref=f"update:{offer}", holdable=True)

    activity.updates_lane(store=seller, now=NOW)

    assert [n["text"] for n in _update_notices(seller)] == ["an earlier update"]


def test_queueing_an_update_and_stamping_its_tells_is_one_transaction(
    seller: Store, monkeypatch
) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    offer = _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1")

    def _crash(*_args, **_kwargs):
        raise RuntimeError("crashed between the notice and the stamp")

    monkeypatch.setattr("sellee.store.updates._stamp_tells_in_txn", _crash)
    with pytest.raises(RuntimeError):
        seller.queue_update_notice("update", ref=f"update:{offer}", tell_ids=[offer])

    assert _update_notices(seller) == []  # the notice rolled back with the stamp
    assert _tell_rows(seller) == {offer: (None, None)}


def test_recording_a_catchup_update_and_stamping_its_tells_is_one_transaction(
    seller: Store, monkeypatch
) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    offer = _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1")

    def _crash(*_args, **_kwargs):
        raise RuntimeError("crashed between the notice and the stamp")

    monkeypatch.setattr("sellee.store.updates._stamp_tells_in_txn", _crash)
    with pytest.raises(RuntimeError):
        seller.record_update_at_catchup("update", ref=f"update:{offer}", tell_ids=[offer])

    assert _update_notices(seller) == []  # the delivered notice rolled back with the stamp
    assert _tell_rows(seller) == {offer: (None, None)}


def test_the_store_refuses_a_second_update_under_the_same_ref(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    offer = _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1")
    first = seller.queue_update_notice("update", ref=f"update:{offer}", tell_ids=[offer])

    again = seller.queue_update_notice("update", ref=f"update:{offer}", tell_ids=[offer])

    assert first is not None and again is None
    assert len(_update_notices(seller)) == 1


def test_an_update_carrying_a_tell_another_notice_already_told_is_not_queued(seller: Store) -> None:
    mixer = _live_item(seller, "KitchenAid mixer")
    first = _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1")
    second = _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o2")
    seller.mark_tells_told([second], notice_id=None, now=NOW)

    queued = seller.queue_update_notice(
        "update", ref=f"update:{first}", tell_ids=[first, second], now=NOW
    )
    recorded = seller.record_update_at_catchup(
        "update", ref=f"update:{first}", tell_ids=[first, second], now=NOW
    )

    assert queued is None and recorded is None
    assert _update_notices(seller) == []
    assert _tell_rows(seller)[first] == (None, None)  # left for the next read to compose


def test_a_catchup_landing_mid_tick_is_not_told_again_by_the_lane(seller: Store) -> None:
    """With decisions, the lane carries only the offer (ref on the offer) while a catchup carries
    everything (ref on the older answered tell), so the refs differ and only the untold check stops
    the lane queueing the offer a second time."""
    seed_setting(seller, "activity_updates", "decisions")
    mixer, desk = _live_item(seller, "KitchenAid mixer"), _live_item(seller, "IKEA desk")
    answered = _tell(seller, desk, "buyer_answered", age=HOUR, thread="fb:a1")
    offer = _tell(seller, mixer, "offer_declined", age=20 * MINUTE, thread="fb:o1")
    queue = seller.queue_update_notice

    def _catchup_first(*args, **kwargs):
        activity.tell_at_catchup(seller, now=NOW)  # lands between the lane's read and its write
        return queue(*args, **kwargs)

    seller.queue_update_notice = _catchup_first  # type: ignore[method-assign]
    activity.updates_lane(store=seller, now=NOW)

    (notice,) = _update_notices(seller)
    assert notice["via"] == "catchup" and notice["ref"] == f"update:{answered}"
    assert _tell_rows(seller) == {answered: (NOW, notice["id"]), offer: (NOW, notice["id"])}


def test_a_catchup_that_loses_the_race_reads_again_for_what_is_left(seller: Store) -> None:
    seed_setting(seller, "activity_updates", "decisions")
    mixer, desk = _live_item(seller, "KitchenAid mixer"), _live_item(seller, "IKEA desk")
    answered = _tell(seller, desk, "buyer_answered", age=HOUR, thread="fb:a1")
    offer = _tell(seller, mixer, "offer_declined", age=20 * MINUTE, thread="fb:o1")
    record = seller.record_update_at_catchup
    calls: list = []

    def _lane_first(*args, **kwargs):
        if not calls:
            activity.updates_lane(store=seller, now=NOW)  # tells the offer in between
        calls.append(kwargs["tell_ids"])
        return record(*args, **kwargs)

    seller.record_update_at_catchup = _lane_first  # type: ignore[method-assign]
    untold = activity.tell_at_catchup(seller, now=NOW)

    assert calls == [(answered, offer), (answered,)]
    assert untold is not None and untold["asks"] is False
    assert "answered a buyer" in untold["text"] and "under your minimum" not in untold["text"]
    lane, catchup = _update_notices(seller)
    assert _tell_rows(seller) == {answered: (NOW, catchup["id"]), offer: (NOW, lane["id"])}


# --- the attended catchup -------------------------------------------------------------------


def test_catchup_tells_what_the_lane_could_not(make_ctx, store: Store) -> None:
    mixer = _live_item(store, "KitchenAid mixer")
    offer = _tell(store, mixer, "offer_declined", age=MINUTE, thread="fb:o1", now=time())

    res = dispatch("get_catchup", {}, make_ctx(TIER_ATTENDED))

    assert res["untold"]["asks"] is True
    assert "under your minimum" in res["untold"]["text"]
    assert leak_paths(res["untold"], FLOOR) == []
    (notice,) = _update_notices(store)
    assert notice["status"] == "delivered" and notice["via"] == "catchup"
    assert notice["controls"] is None  # no channel to tap on; the session asks in words
    assert _tell_rows(store)[offer][1] == notice["id"]
    # handing it over was the telling: the next catchup has nothing untold
    assert dispatch("get_catchup", {}, make_ctx(TIER_ATTENDED))["untold"] is None


def test_catchup_shows_what_the_setting_keeps_off_the_phone(make_ctx, store: Store) -> None:
    """The setting is about what I send unasked; a seller asking what happened is asking."""
    seed_setting(store, "activity_updates", "off")
    desk = _live_item(store, "IKEA desk")
    _tell(store, desk, "buyer_answered", age=MINUTE, thread="fb:a1", now=time())

    res = dispatch("get_catchup", {}, make_ctx(TIER_ATTENDED))

    assert res["untold"]["asks"] is False
    assert "answered a buyer" in res["untold"]["text"]


def test_catchup_with_nothing_untold_says_so(make_ctx, store: Store) -> None:
    res = dispatch("get_catchup", {}, make_ctx(TIER_ATTENDED))

    assert res["untold"] is None
    assert res["counts"]["untold"] == 0
    assert _update_notices(store) == []


def test_catchup_counts_an_untold_update_with_what_is_waiting(make_ctx, store: Store) -> None:
    desk = _live_item(store, "IKEA desk")
    _tell(store, desk, "buyer_answered", age=MINUTE, thread="fb:a1", now=time())

    res = dispatch("get_catchup", {}, make_ctx(TIER_ATTENDED))

    assert res["counts"]["untold"] == 1


def test_the_catchup_tool_says_how_to_hand_over_what_is_untold() -> None:
    """Every prompt that reads the catchup points here, so this is where the rule lives."""
    (spec,) = [s for s in all_specs() if s.name == "get_catchup"]
    assert "pass it on as it reads" in spec.description


# --- /catchup typed in the chat -------------------------------------------------------------


def _phone_catchup(store: Store, bus) -> str:
    text, _controls = fastpaths.handle_fast_path(
        store, bus, {"kind": "command", "text": "/catchup", "payload": {}}
    )
    return text


def _drain(store: Store, bus, sent: list) -> None:
    outbound.drain_notices(
        store=store, bus=bus, deliver=lambda _chat, text, controls: sent.append((text, controls))
    )


def test_phone_catchup_shows_what_off_keeps_from_being_sent(seller: Store, bus) -> None:
    """The setting's own words promise /catchup shows what I did, and the chat is where a
    phone-first seller types it."""
    seed_setting(seller, "activity_updates", "off")
    desk = _live_item(seller, "IKEA desk")
    answered = _tell(seller, desk, "buyer_answered", age=HOUR, thread="fb:a1", now=time())
    activity.updates_lane(store=seller)
    assert _tell_rows(seller)[answered] == (None, None)

    text = _phone_catchup(seller, bus)

    assert text == fastpaths.UNTOLD_FOLLOWS_LINE
    (notice,) = _update_notices(seller)
    assert notice["status"] == "queued" and notice["holdable"] == 0  # the seller asked
    assert notice["controls"] is None  # nothing in it asks
    assert _tell_rows(seller)[answered][1] == notice["id"]
    sent: list = []
    _drain(seller, bus, sent)
    assert [t for t, _ in sent] == [notice["text"]] and "answered a buyer" in notice["text"]
    assert _phone_catchup(seller, bus) == "You're all caught up — nothing waiting."


def test_phone_catchup_shows_what_decisions_held_back(seller: Store, bus) -> None:
    seed_setting(seller, "activity_updates", "decisions")
    desk = _live_item(seller, "IKEA desk")
    _tell(seller, desk, "buyer_answered", age=HOUR, thread="fb:a1", now=time())
    activity.updates_lane(store=seller)
    assert _update_notices(seller) == []

    assert _phone_catchup(seller, bus) == fastpaths.UNTOLD_FOLLOWS_LINE
    (notice,) = _update_notices(seller)
    assert "answered a buyer" in notice["text"]


def test_phone_catchup_sends_an_offer_with_its_buttons(seller: Store, bus) -> None:
    seed_setting(seller, "activity_updates", "off")
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=HOUR, thread="fb:o1", now=time())
    seller.queue_notice("Listing went live.")

    text = _phone_catchup(seller, bus)

    assert text.index("Listing went live.") < text.index(fastpaths.UNTOLD_FOLLOWS_LINE)
    assert "under your minimum" not in text  # it follows as its own message
    sent: list = []
    _drain(seller, bus, sent)
    (update_text, controls) = sent[-1]
    assert "under your minimum" in update_text
    assert [label for label, _token in controls] == list(DECISION_OPTIONS)
    assert leak_paths([text, update_text], FLOOR) == []


def test_a_failed_send_never_loses_what_phone_catchup_owed(seller: Store, bus) -> None:
    """The reply is sent once and a failure is only logged, so the update must not depend on it:
    it waits queued, and a failed delivery leaves it queued for the next one."""
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=MINUTE, thread="fb:o1", now=time())
    _phone_catchup(seller, bus)  # its reply is never delivered

    def _down(_chat, _text, _controls):
        raise RuntimeError("telegram is down")

    with pytest.raises(RuntimeError):
        outbound.drain_notices(store=seller, bus=bus, deliver=_down)
    (notice,) = _update_notices(seller)
    assert notice["status"] == "queued" and notice["attempts"] == 1

    sent: list = []
    _drain(seller, bus, sent)
    assert [t for t, _ in sent] == [notice["text"]]


def test_phone_catchup_while_paused_shows_it_and_records_nothing(seller: Store, bus) -> None:
    """Nothing delivers while paused, so the update is shown in the reply and stays untold."""
    mixer = _live_item(seller, "KitchenAid mixer")
    offer = _tell(seller, mixer, "offer_declined", age=MINUTE, thread="fb:o1", now=time())
    seller.set_paused(True, source="test")

    text = _phone_catchup(seller, bus)

    assert "under your minimum" in text
    assert fastpaths.UNTOLD_FOLLOWS_LINE not in text
    assert _update_notices(seller) == []
    assert _tell_rows(seller) == {offer: (None, None)}
    assert leak_paths(text, FLOOR) == []


def test_phone_catchup_while_paused_sets_the_update_apart_from_the_list_above(
    seller: Store, bus
) -> None:
    """Shown inline under what is queued, the update gets its own paragraph so it does not read
    as the last bullet."""
    mixer = _live_item(seller, "KitchenAid mixer")
    _tell(seller, mixer, "offer_declined", age=MINUTE, thread="fb:o1", now=time())
    seller.queue_notice("Listing went live.")
    seller.set_paused(True, source="test")

    text = _phone_catchup(seller, bus)

    bullet = "• Listing went live."
    assert text.index(bullet) < text.index("under your minimum")
    assert f"{bullet}\n\n" in text


# --- what the channel pass is told about an update's answers ---------------------------------


def _activity_updates_rule() -> str:
    text = skills.load("seller-comms")
    start = text.index("**Activity updates**")
    return text[start : text.index("\n\n", start)]


def test_a_hold_tap_is_still_answered() -> None:
    """A channel pass that sends nothing is filed as failed and the seller gets an error, so Hold
    changes nothing and messages no buyer, but the seller still hears back."""
    rule = _activity_updates_rule()
    assert "nothing is sent" not in rule
    assert "message no buyer" in rule and "say so briefly" in rule


def test_a_bare_number_lowers_a_minimum_only_when_one_listing_was_named() -> None:
    rule = _activity_updates_rule()
    assert "only if the update listed one, else ask which" in rule


def test_the_lane_runs_every_minute_on_the_dot() -> None:
    assert activity.UPDATES_LANE_INTERVAL_SEC == 60.0
    assert activity.HOLD_SEC == 15 * MINUTE
    assert activity.MAX_HOLD_SEC == 90 * MINUTE
