"""The cross-link push: which external-URL set an item's rail listing should carry, when a push
is owed, and what a rail failure leaves behind.

A real store fixture and a fake rail — the push needs no pass, no browser and no scripted
publish, only recorded listing URLs and the marker table.
"""

from __future__ import annotations

import json

from tests.conftest import seed_setting

from sellee import crosslist
from sellee.config import Config
from sellee.rail.client import RailNetworkError, RailUnprovisioned

_RAIL_URL = "https://www.carousell.ai/listing/abc123"
_CAROUSELL_URL = "https://www.carousell.sg/p/teak-lamp-1328307791/"
_FB_URL = "https://www.facebook.com/marketplace/item/555"
_CAROUSELL_ENTRY = {"platform": "EXTERNAL_PLATFORM_CAROUSELL", "url": _CAROUSELL_URL}
_FB_ENTRY = {"platform": "EXTERNAL_PLATFORM_FACEBOOK_MARKETPLACE", "url": _FB_URL}


class FakeRail:
    """Records every update_listing call; raises when told to."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.updates: list = []

    def update_listing(self, listing_id, *, status=None, external_urls=None):
        if self.fail:
            raise RailNetworkError("rail unreachable: timeout")
        self.updates.append((listing_id, external_urls))
        return {"ok": True}


def _deps(store, bus, rail):
    return crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=lambda: rail,
    )


def _item(store, urls):
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    for market, url in urls.items():
        store.record_listing_url(item["id"], market, url)
    return store.get_item(item["id"])


def _events(bus, kind):
    return bus.store.read(kinds=[kind])


# --- the desired set ----------------------------------------------------------------------------


def test_only_mapped_markets_join_the_set() -> None:
    urls = {
        "carousell-ai": _RAIL_URL,  # the rail itself is never in its own set
        "carousell": _CAROUSELL_URL,
        "mercari": "https://www.mercari.com/item/1",  # recordable, but no rail platform for it
    }
    assert crosslist.desired_external_urls(urls) == [_CAROUSELL_ENTRY]


def test_the_set_is_platform_sorted_whatever_the_dict_order() -> None:
    forward = {"carousell": _CAROUSELL_URL, "fb": _FB_URL}
    backward = {"fb": _FB_URL, "carousell": _CAROUSELL_URL}
    assert crosslist.desired_external_urls(forward) == [_CAROUSELL_ENTRY, _FB_ENTRY]
    assert crosslist.desired_external_urls(forward) == crosslist.desired_external_urls(backward)


def test_a_mapped_market_with_an_empty_url_is_left_out() -> None:
    assert crosslist.desired_external_urls({"carousell": ""}) == []


def test_the_market_platform_map_is_injective_and_names_known_enum_values() -> None:
    """The rail rejects a set carrying one platform twice, so two markets must never share one —
    and the values are pinned to the proto's enum names, the shape protojson accepts."""
    values = list(crosslist.MARKET_PLATFORMS.values())
    assert len(values) == len(set(values))
    assert set(values) <= {
        "EXTERNAL_PLATFORM_CAROUSELL",
        "EXTERNAL_PLATFORM_FACEBOOK_MARKETPLACE",
    }


# --- when a push is owed ------------------------------------------------------------------------


def test_a_cross_listed_item_gets_one_push_and_a_marker(store, bus) -> None:
    item = _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    rail = FakeRail()

    assert crosslist.push_crosslinks(_deps(store, bus, rail)) == 1
    assert rail.updates == [("abc123", {"urls": [_CAROUSELL_ENTRY]})]
    assert store.crosslink_pushed_urls() == {
        item["id"]: json.dumps([_CAROUSELL_ENTRY], sort_keys=True)
    }
    (event,) = _events(bus, "crosslink.pushed")
    assert event.payload == {
        "item_id": item["id"],
        "listing_id": "abc123",
        "platforms": ["EXTERNAL_PLATFORM_CAROUSELL"],
    }


def test_a_matching_marker_means_no_call(store, bus) -> None:
    _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    rail = FakeRail()
    deps = _deps(store, bus, rail)

    assert crosslist.push_crosslinks(deps) == 1
    assert crosslist.push_crosslinks(deps) == 0
    assert len(rail.updates) == 1  # steady state costs nothing


def test_a_changed_set_is_pushed_again(store, bus) -> None:
    item = _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    rail = FakeRail()
    deps = _deps(store, bus, rail)
    crosslist.push_crosslinks(deps)

    store.record_listing_url(item["id"], "fb", _FB_URL)
    assert crosslist.push_crosslinks(deps) == 1
    assert rail.updates[-1] == ("abc123", {"urls": [_CAROUSELL_ENTRY, _FB_ENTRY]})


def test_a_set_that_emptied_is_pushed_as_empty(store, bus) -> None:
    """Present-but-empty replaces the rail's whole set with nothing — the honest half of
    replace-whole-set semantics, live the day something starts removing browser URLs."""
    item = _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    rail = FakeRail()
    deps = _deps(store, bus, rail)
    crosslist.push_crosslinks(deps)

    store.archive_listing_url(item["id"], "carousell")
    assert crosslist.push_crosslinks(deps) == 1
    assert rail.updates[-1] == ("abc123", {"urls": []})
    assert store.crosslink_pushed_urls() == {item["id"]: "[]"}


def test_nothing_to_push_writes_no_row_and_makes_no_call(store, bus) -> None:
    _item(store, {"carousell-ai": _RAIL_URL})
    rail = FakeRail()

    assert crosslist.push_crosslinks(_deps(store, bus, rail)) == 0
    assert rail.updates == []
    assert store.crosslink_pushed_urls() == {}


# --- who is excluded ----------------------------------------------------------------------------


def test_an_item_with_no_rail_listing_is_skipped(store, bus) -> None:
    _item(store, {"carousell": _CAROUSELL_URL})
    rail = FakeRail()
    assert crosslist.push_crosslinks(_deps(store, bus, rail)) == 0
    assert rail.updates == []


def test_a_rail_url_that_does_not_parse_is_skipped(store, bus) -> None:
    _item(store, {"carousell-ai": "https://www.carousell.ai/u/chat", "carousell": _CAROUSELL_URL})
    rail = FakeRail()
    assert crosslist.push_crosslinks(_deps(store, bus, rail)) == 0
    assert rail.updates == []
    assert _events(bus, "crosslink.push_failed") == []  # a skip, not a failure


def test_a_sold_item_is_not_pushed(store, bus) -> None:
    """Its rail listing is about to be archived — a push would race the take-down for a link
    nobody will render."""
    item = _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    store.create_thread(
        thread_id="carousell:1",
        side="sell",
        market="carousell",
        counterpart_handle="buyer",
        item_id=item["id"],
    )
    store.negotiate_confirm_sold(item["id"], "carousell:1")

    rail = FakeRail()
    assert crosslist.push_crosslinks(_deps(store, bus, rail)) == 0
    assert rail.updates == []


# --- failure is silent-retry --------------------------------------------------------------------


def test_a_rail_failure_leaves_the_marker_untouched_and_retries_next_tick(store, bus) -> None:
    item = _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    failing = FakeRail(fail=True)

    assert crosslist.push_crosslinks(_deps(store, bus, failing)) == 0
    assert store.crosslink_pushed_urls() == {}
    (event,) = _events(bus, "crosslink.push_failed")
    assert event.payload["item_id"] == item["id"]
    assert "unreachable" in event.payload["reason"]
    assert store.claim_queued_notices(10) == []  # no needs-me notice: the seller cannot act on it

    # The next tick sees the same mismatch and pushes — nothing was consumed by the failure.
    recovered = FakeRail()
    assert crosslist.push_crosslinks(_deps(store, bus, recovered)) == 1
    assert recovered.updates == [("abc123", {"urls": [_CAROUSELL_ENTRY]})]


def test_an_unprovisioned_rail_skips_the_phase_silently(store, bus) -> None:
    _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})

    def factory():
        raise RailUnprovisioned("carousell.ai is not provisioned")

    deps = crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=factory,
    )
    assert crosslist.push_crosslinks(deps) == 0
    assert _events(bus, "crosslink.push_failed") == []
    assert store.crosslink_pushed_urls() == {}


# --- through the lane ---------------------------------------------------------------------------


def _noon() -> float:
    import time as _time

    stamp = _time.localtime()
    return _time.mktime(
        (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 12, 0, 0, stamp.tm_wday, stamp.tm_yday, -1)
    )


def _midnight() -> float:
    import time as _time

    stamp = _time.localtime()
    return _time.mktime(
        (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 2, 0, 0, stamp.tm_wday, stamp.tm_yday, -1)
    )


def _lane_deps(store, bus, rail, now=_noon):
    # Nothing connected, so a lane tick here is only the push phase. That is what these tests are
    # about, and it is the sharper form of their own claim: the cross-link is earned by where the
    # item actually is, never by the fan-out setting. Left connected, the tick would also try to
    # publish, which needs a browser these tests have no reason to provide.
    seed_setting(store, "connected_markets", [])
    return crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=lambda: rail,
        now=now,
    )


def test_one_lane_tick_links_a_cross_published_item(store, bus) -> None:
    """The plan's exit shape at lane level: publish on both, one tick links them, a second tick
    is free, and the take-down ends it — no push ever fires for that item again."""
    item = _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    rail = FakeRail()

    crosslist.crosslist_lane(_lane_deps(store, bus, rail))
    assert rail.updates == [("abc123", {"urls": [_CAROUSELL_ENTRY]})]
    assert store.crosslink_pushed_urls() == {
        item["id"]: json.dumps([_CAROUSELL_ENTRY], sort_keys=True)
    }

    crosslist.crosslist_lane(_lane_deps(store, bus, rail))
    assert len(rail.updates) == 1

    # The take-down's effect: the rail URL is archived, and with it this item's eligibility.
    store.archive_listing_url(item["id"], "carousell-ai")
    crosslist.crosslist_lane(_lane_deps(store, bus, rail))
    assert len(rail.updates) == 1


def test_a_paused_agent_pushes_nothing(store, bus) -> None:
    _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    store.set_paused(True)
    rail = FakeRail()

    crosslist.crosslist_lane(_lane_deps(store, bus, rail))
    assert rail.updates == []


def test_quiet_hours_do_not_hold_the_push(store, bus) -> None:
    """The asymmetry against the enqueue phase, chosen not forgotten: a push is an API call on our
    own rail, not activity on the seller's marketplace account."""
    seed_setting(store, "quiet_hours", [2300, 800])
    _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    rail = FakeRail()

    crosslist.crosslist_lane(_lane_deps(store, bus, rail, now=_midnight))
    assert len(rail.updates) == 1


def test_the_push_needs_no_crosslist_setting(store, bus) -> None:
    """Eligibility derives from where the item actually is, never from connected_markets — an
    attended publish outside the fan-out still earns the link."""
    item = _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL})
    rail = FakeRail()

    crosslist.crosslist_lane(_lane_deps(store, bus, rail))
    assert rail.updates == [("abc123", {"urls": [_CAROUSELL_ENTRY]})]
    assert item["id"] in store.crosslink_pushed_urls()
