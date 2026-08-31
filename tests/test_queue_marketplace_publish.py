"""queue_marketplace_publish: the way back into a fan-out that has spent its one attempt.

The lane refuses to retry a settled (item, market) pair forever, which is right for automatic work
and wrong once the seller asks. These tests pin what the tool skips (the one-shot, and only that)
and what it still refuses — above all, listing the same item twice on one marketplace.
"""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

import sellee.tools  # noqa: F401  registration
from sellee import crosslist
from sellee.browser.client import BrowserUnavailable
from sellee.tools.registry import TIER_PASS_CHANNEL, ToolError, dispatch

_RAIL_URL = "https://www.carousell.ai/listing/abc123"
_CAROUSELL_URL = "https://www.carousell.sg/p/teak-lamp-1328307791/"


@pytest.fixture
def enabled(store):
    """A seller in SG with Carousell turned on, and one item live on the rail."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell-ai", _RAIL_URL)
    return store.get_item(item["id"])


def _ctx(make_ctx, **kw):
    kw.setdefault("browser_factory", lambda: object())
    return make_ctx(TIER_PASS_CHANNEL, pass_id="p1", **kw)


def _call(ctx, item_id, market="carousell"):
    return dispatch("queue_marketplace_publish", {"item_id": item_id, "market": market}, ctx)


def _spent_the_shot(store, item_id, market="carousell"):
    """Settle a failed attempt, which is what ends the lane's automatic retries for the pair."""
    pass_id = store.enqueue_pass(
        "publish", {"item_id": item_id, "market": market, "origin": crosslist.ORIGIN}
    )
    store.finish_pass(pass_id, status="error", rc=1, cls="error", summary="error")


# --- the retry itself ---------------------------------------------------------------------------


def test_it_queues_a_publish_the_lane_would_never_queue_again(make_ctx, store, enabled) -> None:
    # The pair is spent: test_crosslist_lane.py pins that the lane will not touch it again.
    _spent_the_shot(store, enabled["id"])

    result = _call(_ctx(make_ctx), enabled["id"])
    assert result["status"] == "queued"
    queued = [row for row in store.publish_pass_index() if row["status"] == "queued"]
    assert queued == [
        {
            "market": "carousell",
            "item_id": enabled["id"],
            "origin": "crosslist",
            "status": "queued",
        }
    ]


def test_the_queued_pass_owes_the_seller_a_report(make_ctx, store, bus, enabled) -> None:
    """The origin marker is what makes the daemon report the outcome. Without it the seller asks
    for a retry, hears "starting", and never hears anything again — the pass takes minutes and the
    conversation that asked is long over."""
    _call(_ctx(make_ctx), enabled["id"])
    pass_id = [row for row in store.publish_pass_index() if row["status"] == "queued"]
    assert pass_id  # queued above

    settled = store.unreported_crosslist_passes()
    assert settled == []  # still running, nothing to report yet

    # Settle it as a failure and the ordinary sweep picks it up, no special path.
    running = store._db.query("SELECT pass_id FROM passes WHERE status = 'queued'")[0]["pass_id"]
    store.finish_pass(running, status="error", rc=1, cls="error", summary="error")
    assert [row["pass_id"] for row in store.unreported_crosslist_passes()] == [running]


def test_it_announces_the_queue_on_the_bus(make_ctx, store, bus, enabled) -> None:
    _call(_ctx(make_ctx), enabled["id"])
    (event,) = bus.store.read(kinds=["crosslist.queued"])
    assert event.payload == {"item_id": enabled["id"], "market": "carousell"}


# --- never twice --------------------------------------------------------------------------------


def test_an_item_already_listed_there_is_not_published_again(make_ctx, store, enabled) -> None:
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)

    result = _call(_ctx(make_ctx), enabled["id"])
    assert result == {
        "status": "already_listed",
        "item_id": enabled["id"],
        "market": "carousell",
        "url": _CAROUSELL_URL,
    }
    assert store.publish_pass_index() == []


def test_asking_twice_queues_one_publish(make_ctx, store, enabled) -> None:
    """The duplicate that would actually cost the seller something: two passes, two live listings
    for one item, on their own account."""
    ctx = _ctx(make_ctx)
    assert _call(ctx, enabled["id"])["status"] == "queued"
    assert _call(ctx, enabled["id"])["status"] == "already_queued"
    assert len(store.publish_pass_index()) == 1


def test_a_publish_already_running_is_not_joined_by_a_second(make_ctx, store, enabled) -> None:
    pass_id = store.enqueue_pass("publish", {"item_id": enabled["id"], "market": "carousell"})
    store.claim_queued_pass()  # -> running

    assert _call(_ctx(make_ctx), enabled["id"])["status"] == "already_queued"
    assert [row["status"] for row in store.publish_pass_index()] == ["running"]
    assert pass_id


# --- what it still refuses ------------------------------------------------------------------------


def test_a_marketplace_the_seller_has_not_turned_on_is_refused(make_ctx, store, enabled) -> None:
    """Enabling a marketplace is an approval-gated settings change, because it lets the agent post
    publicly as them. A retry rides on that approval; it does not stand in for it."""
    with pytest.raises(ToolError, match="not an enabled marketplace"):
        _call(_ctx(make_ctx), enabled["id"], market="fb")
    assert store.publish_pass_index() == []


def test_a_stale_market_the_sellers_region_lost_is_refused(make_ctx, store, enabled) -> None:
    """The stored setting still names Carousell, but a US account has nowhere to be listed there —
    the same filter the lane's eligibility uses."""
    store.set_seller_config_section("basics", {"region": "US"})
    with pytest.raises(ToolError, match="not an enabled marketplace"):
        _call(_ctx(make_ctx), enabled["id"])


def test_an_item_not_on_the_rail_is_refused(make_ctx, store) -> None:
    """Rail-first, the same precondition the lane's eligibility rests on."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")

    with pytest.raises(ToolError, match="not published on carousell.ai"):
        _call(_ctx(make_ctx), item["id"])
    assert store.publish_pass_index() == []


def test_an_unknown_item_is_refused(make_ctx, store, enabled) -> None:
    with pytest.raises(ToolError, match="no item with id"):
        _call(_ctx(make_ctx), "item_nope")


def test_a_sold_item_is_not_published(make_ctx, store, enabled) -> None:
    store.create_thread(
        thread_id="carousell:1",
        side="sell",
        market="carousell",
        counterpart_handle="buyer",
        item_id=enabled["id"],
    )
    store.negotiate_confirm_sold(enabled["id"], "carousell:1")

    assert _call(_ctx(make_ctx), enabled["id"])["status"] == "sold"
    assert store.publish_pass_index() == []


def test_a_paused_agent_publishes_nothing(make_ctx, store, enabled) -> None:
    store.set_paused(True)
    with pytest.raises(ToolError, match="paused"):
        _call(_ctx(make_ctx), enabled["id"])
    assert store.publish_pass_index() == []


def test_no_browser_is_answered_in_the_conversation_not_by_a_doomed_pass(
    make_ctx, store, enabled
) -> None:
    """Checked before queueing: the seller asked just now, so the reason they can't have it should
    reach them now, with the acquisition's own wording."""

    def unavailable():
        raise BrowserUnavailable("'npx' is not installed")

    with pytest.raises(ToolError, match="npx"):
        _call(_ctx(make_ctx, browser_factory=unavailable), enabled["id"])
    assert store.publish_pass_index() == []


# --- who can call it ------------------------------------------------------------------------------


def test_the_buyer_facing_reply_pass_cannot_publish(make_ctx, store, enabled) -> None:
    """A publish is a public post on the seller's account; nothing a buyer says should reach it."""
    from sellee.tools.registry import TIER_PASS_REPLY, UnknownTool

    ctx = make_ctx(TIER_PASS_REPLY, pass_id="p1", browser_factory=lambda: object())
    with pytest.raises(UnknownTool):
        _call(ctx, enabled["id"])
