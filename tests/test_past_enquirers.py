"""The update planner, driven over the store's own read and ledger with generated thread
statuses, transcripts, follow-up stamps and negotiation states."""

from __future__ import annotations

import itertools
import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sellee import marketplaces
from sellee.engines.negotiate import NOT_STANDING
from sellee.past_enquirers import (
    ListingChange,
    format_price,
    plan_updates,
)
from sellee.store.updates import announcement
from sellee.tools.reply import _refused

RAIL = marketplaces.RAIL
_SELL_STATUSES = (
    "active",
    "liaising",
    "agreed",
    "seller_handling",
    "handover",
    "lost",
    "closed",
    "escalated",
    "held",
)
_LEDGER_STATUSES = ("active", "front_runner", "leading_bid", "outbid", *NOT_STANDING)
_T0 = 1_700_000_000.0
_ids = itertools.count()


# --- the scenario --------------------------------------------------------------------------------

_thread = st.fixed_dictionaries(
    {
        # Weighted towards an open, answered thread, so the ledger rules get exercised.
        "status": st.sampled_from(_SELL_STATUSES) | st.just("active"),
        "buyer_wrote": st.booleans() | st.just(True),
        # Where the reply cursor sits: on the newest buyer message, behind it, or unset.
        "cursor": st.sampled_from(("newest", "behind", "none")) | st.just("newest"),
        # A nudge, and a past-enquirer update, each before or after the buyer's newest message.
        "followup": st.sampled_from(("none", "before", "after")),
        "update": st.sampled_from(("none", "before", "after")),
        # A lowered minimum reopened this buyer and owes them a reply.
        "owed": st.booleans(),
        "ledger": st.none()
        | st.fixed_dictionaries(
            {
                "status": st.sampled_from(_LEDGER_STATUSES),
                "highest_offer": st.sampled_from((0.0, 50.0, 79.0, 80.0, 81.0, 200.0)),
            }
        ),
    }
)
_price = st.sampled_from((50.0, 79.5, 80.0, 95.0, 120.0))
_title = st.text(
    alphabet=st.characters(categories=("L", "N", "Zs", "P"), exclude_characters="{}"),
    min_size=1,
    max_size=30,
).filter(str.strip)  # the store refuses a blank title


def _execute(store, sql: str, params: tuple) -> None:
    with store._db.transaction() as conn:  # noqa: SLF001 — tests may arrange store state directly
        conn.execute(sql, params)


def _seed(store, item_id: str, market: str, spec: dict) -> str:
    thread_id = f"{market}:t{next(_ids)}"
    store.create_thread(
        thread_id=thread_id, side="sell", market=market, counterpart_handle="b", item_id=item_id
    )
    newest = None
    if spec["buyer_wrote"]:
        store.append_thread_message(thread_id, msg_id="m1", direction="in", text="hi", ts=_T0)
        store.append_thread_message(thread_id, msg_id="m2", direction="in", text="?", ts=_T0 + 10)
        newest = ("m2", _T0 + 10)
    cursor = {
        "newest": newest,
        "behind": ("m1", _T0) if newest else None,
        "none": None,
    }[spec["cursor"]]
    stamps = {"none": None, "before": _T0 + 5, "after": _T0 + 20}
    followup = stamps[spec["followup"]]
    update = stamps[spec.get("update", "none")]
    _execute(
        store,
        "UPDATE threads SET status = ?, cursor_last_msg_id = ?, cursor_last_ts = ?, "
        "last_followup_ts = ?, last_update_ts = ?, reply_owed_ts = ? WHERE thread_id = ?",
        (
            spec["status"],
            cursor[0] if cursor else None,
            cursor[1] if cursor else None,
            followup,
            update,
            _T0 + 30 if spec["owed"] else None,
            thread_id,
        ),
    )
    if spec["ledger"] is not None:
        _execute(
            store,
            "INSERT OR IGNORE INTO negotiations (item_id, state, updated_ts) VALUES (?, 'open', ?)",
            (item_id, _T0),
        )
        _execute(
            store,
            "INSERT INTO negotiation_buyers (item_id, thread_id, offers, highest_offer, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                item_id,
                thread_id,
                json.dumps([spec["ledger"]["highest_offer"]]),
                spec["ledger"]["highest_offer"],
                spec["ledger"]["status"],
            ),
        )
    return thread_id


def _plan(store, change: ListingChange):
    return plan_updates(
        change,
        store.item_threads(change.item_id, change.market),
        store.negotiate_status(change.item_id)["buyers"],
    )


def _may_hear(spec: dict, new_price: float) -> bool:
    """The planner's rules, restated over the generated facts."""
    ledger = spec["ledger"] or {}
    standing = ledger.get("status") not in NOT_STANDING
    # An owing counts only while its buyer stands in the ledger (store/helpers._LIVE_OWING_SQL).
    owed = spec["owed"] and bool(ledger)
    return (
        not owed
        and not _refused("sell", spec["status"], "followup")
        and spec["buyer_wrote"]
        and spec["cursor"] == "newest"
        and spec["update"] != "after"  # a nudge after it does not count
        and standing
        and not (ledger.get("highest_offer", 0) > 0 and ledger["highest_offer"] >= new_price)
    )


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    specs=st.lists(_thread, min_size=0, max_size=6),
    old=_price,
    new=_price,
    title=_title,
    currency=st.sampled_from(("SGD", "USD", "MYR", None)),
)
def test_the_planner_picks_exactly_the_buyers_the_rules_allow(
    store, specs, old, new, title, currency
) -> None:
    item = store.create_item(title=title, list_price=new, currency=currency or "SGD")
    other = store.create_item(title="Other", list_price=new, currency="SGD")
    by_thread = {_seed(store, item["id"], RAIL, spec): spec for spec in specs}
    # Decoys: the same item on another market, and another item on this one.
    decoys = {_seed(store, item["id"], "fb", spec) for spec in specs}
    decoys |= {_seed(store, other["id"], RAIL, spec) for spec in specs}
    change = ListingChange(item["id"], RAIL, title, currency, old, new)

    planned = _plan(store, change)
    chosen = {u.thread_id for u in planned}

    assert not chosen & decoys
    if not new < old or currency is None:
        assert planned == []  # a rise, an unchanged price, or no currency to name plans nothing
        return
    assert chosen == {t for t, spec in by_thread.items() if _may_hear(spec, new)}
    assert len(planned) == len(chosen)  # one message per thread
    drop = f"is now {format_price(new, currency)} (was {format_price(old, currency)})"
    for update in planned:
        assert update.in_msg_id == "m2"
        assert update.text == f"The price of the {title} {drop}."

    # Sent and recorded through the store's own send bracket, the same change selects nobody.
    for update in planned:
        reserved = store.reserve_reply(
            thread_id=update.thread_id,
            kind="followup",
            text=update.text,
            in_msg_id=update.in_msg_id,
            cfg=None,
            announces=announcement(price_drop=True, new_photos=False),
        )
        store.commit_reply(
            intent_id=reserved["intent_id"],
            thread_id=update.thread_id,
            in_msg_id=update.in_msg_id,
            text=update.text,
            kind="followup",
        )
    assert _plan(store, change) == []


# --- worked examples -----------------------------------------------------------------------------


def _change(**kw) -> ListingChange:
    base = dict(
        item_id="i1",
        market=RAIL,
        title="Sony headphones",
        currency="SGD",
        old_price=95.0,
        new_price=80.0,
    )
    base.update(kw)
    return ListingChange(**base)


def _row(**kw) -> dict:
    base = dict(
        thread_id=f"{RAIL}:t1",
        side="sell",
        market=RAIL,
        item_id="i1",
        status="active",
        last_inbound_msg_id="m2",
        last_inbound_ts=_T0,
        cursor_last_ts=_T0,
        last_followup_ts=None,
        last_update_ts=None,
        reply_owed_ts=None,
    )
    base.update(kw)
    return base


def test_the_price_drop_message_reads_as_agreed() -> None:
    [update] = plan_updates(_change(), [_row()], {})
    assert update.text == "The price of the Sony headphones is now $80 (was $95)."


@pytest.mark.parametrize(
    ("amount", "currency", "shown"),
    [
        (80.0, "SGD", "$80"),
        (79.5, "USD", "$79.50"),
        (1250.0, "sgd", "$1,250"),
        (80.0, "MYR", "80 MYR"),
        (79.125, "SGD", "$79.13"),  # half-up, as money.py rounds
    ],
)
def test_prices_are_written_the_way_a_buyer_reads_them(amount, currency, shown) -> None:
    assert format_price(amount, currency) == shown


@pytest.mark.parametrize("currency", [None, "", "dollars", "S$"])
def test_a_price_with_no_currency_to_name_tells_nobody(currency) -> None:
    assert plan_updates(_change(currency=currency), [_row()], {}) == []


def test_a_nudge_sent_since_the_buyer_wrote_does_not_hold_back_a_price_drop() -> None:
    [update] = plan_updates(_change(), [_row(last_followup_ts=_T0 + 5)], {})
    assert update.thread_id == f"{RAIL}:t1"


def test_a_buyer_whose_offer_meets_the_new_price_is_left_to_the_negotiation() -> None:
    ledger = {f"{RAIL}:t1": {"status": "active", "highest_offer": 80.0}}
    assert plan_updates(_change(), [_row()], ledger) == []


def test_item_threads_reads_one_items_threads_on_one_market_with_the_newest_buyer_message(
    store,
) -> None:
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    other = store.create_item(title="Desk", list_price=80.0, currency="SGD")
    for thread_id, item_id, market in (
        (f"{RAIL}:a", item["id"], RAIL),
        (f"{RAIL}:b", item["id"], RAIL),
        ("fb:c", item["id"], "fb"),
        (f"{RAIL}:d", other["id"], RAIL),
    ):
        store.create_thread(
            thread_id=thread_id, side="sell", market=market, counterpart_handle="x", item_id=item_id
        )
    store.append_thread_message(f"{RAIL}:a", msg_id="in1", direction="in", text="hi", ts=_T0)
    store.append_thread_message(f"{RAIL}:a", msg_id="in2", direction="in", text="?", ts=_T0 + 2)
    store.append_thread_message(f"{RAIL}:a", msg_id="out1", direction="out", text="ok", ts=_T0 + 3)

    rows = {r["thread_id"]: r for r in store.item_threads(item["id"], RAIL)}

    assert set(rows) == {f"{RAIL}:a", f"{RAIL}:b"}
    assert (rows[f"{RAIL}:a"]["last_inbound_msg_id"], rows[f"{RAIL}:a"]["last_inbound_ts"]) == (
        "in2",
        _T0 + 2,
    )
    assert rows[f"{RAIL}:b"]["last_inbound_msg_id"] is None
    assert rows[f"{RAIL}:b"]["last_inbound_ts"] is None


def test_item_threads_reads_a_reply_owed_only_while_its_buyer_still_stands(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    owed = {"status": "active", "highest_offer": 70.0}
    spec = {"status": "active", "buyer_wrote": True, "cursor": "newest", "followup": "none"}
    standing = _seed(store, item["id"], RAIL, {**spec, "owed": True, "ledger": owed})
    withdrew = _seed(
        store, item["id"], RAIL, {**spec, "owed": True, "ledger": {**owed, "status": "withdrew"}}
    )
    plain = _seed(store, item["id"], RAIL, {**spec, "owed": False, "ledger": owed})

    rows = {r["thread_id"]: r["reply_owed_ts"] for r in store.item_threads(item["id"], RAIL)}

    assert rows == {standing: _T0 + 30, withdrew: None, plain: None}


def test_a_scoped_session_reads_only_its_own_threads_of_an_item_in_scope(store) -> None:
    from sellee.store import Scope, ScopedStore

    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    other = store.create_item(title="Desk", list_price=80.0, currency="SGD")
    for thread_id, item_id in (
        (f"{RAIL}:a", item["id"]),
        (f"{RAIL}:b", item["id"]),
        (f"{RAIL}:c", other["id"]),
    ):
        store.create_thread(
            thread_id=thread_id, side="sell", market=RAIL, counterpart_handle="x", item_id=item_id
        )

    scope = Scope.of(threads=[f"{RAIL}:a", f"{RAIL}:c"], items=[item["id"]])
    scoped = ScopedStore(store, scope)

    assert [r["thread_id"] for r in scoped.item_threads(item["id"], RAIL)] == [f"{RAIL}:a"]
    assert scoped.item_threads(other["id"], RAIL) == []
    assert len(ScopedStore(store, None).item_threads(item["id"], RAIL)) == 2


@pytest.mark.parametrize(
    "row",
    [
        _row(market="fb"),
        _row(item_id="i2"),
        _row(side="buy"),
        _row(last_update_ts=_T0),  # an update at the very moment of the buyer's last message
        _row(reply_owed_ts=_T0 + 5),  # a lowered minimum already owes this buyer a reply
    ],
    ids=[
        "another market",
        "another item",
        "a buy thread",
        "updated as the buyer wrote",
        "owed a reply",
    ],
)
def test_the_planner_skips_a_thread_that_is_not_this_changes_to_tell(row) -> None:
    assert plan_updates(_change(), [row], {}) == []


@pytest.mark.parametrize("status", _SELL_STATUSES)
def test_only_an_open_conversation_hears_about_a_price_drop(status) -> None:
    hears = status in ("active", "liaising")
    assert bool(plan_updates(_change(), [_row(status=status)], {})) is hears


@pytest.mark.parametrize(
    ("offer", "old", "new", "hears"),
    [
        (50.0, 95.0, 80.0, True),  # a standing offer below the new price
        (0.0, 0.95, 0.5, True),  # no offer at all, even under a dollar
        (80.0, 95.0, 80.0, False),
        (81.0, 95.0, 80.0, False),
        (0.75, 0.95, 0.5, False),  # an offer under a dollar still stands
    ],
)
def test_a_standing_offer_below_the_new_price_still_hears(offer, old, new, hears) -> None:
    ledger = {f"{RAIL}:t1": {"status": "active", "highest_offer": offer}}
    change = _change(old_price=old, new_price=new)
    assert bool(plan_updates(change, [_row()], ledger)) is hears
