"""The cross-link push: which external-URL set an item's rail listing should carry, when a push
is owed, and what a rail failure leaves behind.

A real store fixture and a fake rail — the push needs no pass, no browser and no scripted
publish, only recorded listing URLs and the marker table.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.conftest import connect_craigslist, seed_setting

from sellee import craigslist_posts, crosslist
from sellee.browser.markets import craigslist
from sellee.config import Config
from sellee.rail.client import RailNetworkError, RailToolRefused, RailUnprovisioned

_RAIL_URL = "https://www.carousell.ai/listing/abc123"
_CAROUSELL_URL = "https://www.carousell.sg/p/teak-lamp-1328307791/"
_FB_URL = "https://www.facebook.com/marketplace/item/555"
_CL_URL = "https://www.craigslist.org/view/d/teak-lamp/9TXkjafxYMDS5VaPLGdRyk"
_CL_REPOST = "https://www.craigslist.org/view/d/teak-lamp/4bQyZp8RmLwT2sVnK6cDhe"
_CL_MANAGE = "https://post.craigslist.org/manage/9TXkjafxYMDS5VaPLGdRyk"
_CAROUSELL_ENTRY = {"platform": "EXTERNAL_PLATFORM_CAROUSELL", "url": _CAROUSELL_URL}
_FB_ENTRY = {"platform": "EXTERNAL_PLATFORM_FACEBOOK_MARKETPLACE", "url": _FB_URL}
_CL_ENTRY = {"platform": "EXTERNAL_PLATFORM_CRAIGSLIST", "url": _CL_URL}
_PROPERTY = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


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
        "EXTERNAL_PLATFORM_CRAIGSLIST",
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
    # Nothing connected, so a lane tick is only the push phase; connected, the tick would also
    # try to publish, which needs a browser these tests have no reason to provide.
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


# --- Craigslist ---------------------------------------------------------------------------------


def test_a_confirmed_craigslist_post_is_pushed(store, bus) -> None:
    _item(store, {"carousell-ai": _RAIL_URL, "craigslist": _CL_URL})
    rail = FakeRail()

    assert crosslist.push_crosslinks(_deps(store, bus, rail)) == 1
    assert rail.updates == [("abc123", {"urls": [_CL_ENTRY]})]


def test_a_removed_post_drops_out_and_a_repost_replaces_it(store, bus) -> None:
    item = _item(store, {"carousell-ai": _RAIL_URL, "craigslist": _CL_URL})
    rail = FakeRail()
    deps = _deps(store, bus, rail)
    crosslist.push_crosslinks(deps)

    craigslist_posts.check(store, bus, item, lambda _url: craigslist.POST_FLAGGED)
    crosslist.push_crosslinks(deps)
    assert rail.updates[-1] == ("abc123", {"urls": []})

    store.record_listing_url(item["id"], "craigslist", _CL_REPOST)
    crosslist.push_crosslinks(deps)
    assert rail.updates[-1] == (
        "abc123",
        {"urls": [{"platform": "EXTERNAL_PLATFORM_CRAIGSLIST", "url": _CL_REPOST}]},
    )


def test_a_sold_items_craigslist_post_is_not_pushed(store, bus) -> None:
    item = _item(store, {"carousell-ai": _RAIL_URL, "craigslist": _CL_URL})
    store.negotiate_confirm_sold(item["id"], "carousell:1")
    rail = FakeRail()

    assert crosslist.push_crosslinks(_deps(store, bus, rail)) == 0
    assert rail.updates == []


def test_an_item_sold_after_its_push_is_not_pushed_again(store, bus) -> None:
    """The sale closes the post too, but the emptied set would race the rail listing's take-down."""
    item = _item(store, {"carousell-ai": _RAIL_URL, "craigslist": _CL_URL})
    rail = FakeRail()
    deps = _deps(store, bus, rail)
    crosslist.push_crosslinks(deps)

    store.negotiate_confirm_sold(item["id"], "carousell:1")
    store.archive_listing_url(item["id"], "craigslist")
    assert crosslist.push_crosslinks(deps) == 0
    assert rail.updates == [("abc123", {"urls": [_CL_ENTRY]})]


def test_a_post_on_the_sellers_own_craigslist_account_is_pushed(store, bus) -> None:
    """The push reads no account state, so a seller on their own account gets the link too."""
    store.craigslist_signed_in(
        activated_notice="connected",
        activated_controls=[],
        back_in_notice="back in",
        own_email="seller@example.com",
        own_notice="own account",
    )
    _item(store, {"carousell-ai": _RAIL_URL, "craigslist": _CL_URL})
    rail = FakeRail()

    crosslist.push_crosslinks(_deps(store, bus, rail))
    assert rail.updates == [("abc123", {"urls": [_CL_ENTRY]})]


@pytest.mark.parametrize(
    "url",
    [
        _CL_MANAGE,
        "https://sfbay.craigslist.org/sfc/fuo/d/teak-lamp/7712345678.html",
        "https://www.craigslist.org.evil.com/view/d/teak-lamp/9TXkjafxYMDS5VaPLGdRyk",
        "https://www.craigslist.org@evil.com/view/d/teak-lamp/9TXkjafxYMDS5VaPLGdRyk",
        _CL_URL + "?lang=en",
        _CL_URL + "#manage",
        _CL_URL.replace("https://", "http://"),
        _CL_URL + "/",
        _CL_URL + "\n",
        "javascript:alert(1)//www.craigslist.org/view/d/teak-lamp/9TXkjafxYMDS5VaPLGdRyk",
    ],
    ids=[
        "manage",
        "regional-html",
        "host-suffix",
        "userinfo",
        "query",
        "fragment",
        "http",
        "trailing-slash",
        "newline",
        "javascript",
    ],
)
def test_a_craigslist_url_that_is_not_the_post_link_is_never_pushed(store, bus, url) -> None:
    """Only the one pinned /view/d/ shape goes public; the manage page shares the post's token."""
    connect_craigslist(store)
    _item(store, {"carousell-ai": _RAIL_URL, "carousell": _CAROUSELL_URL, "craigslist": url})
    rail = FakeRail()

    crosslist.push_crosslinks(_deps(store, bus, rail))
    assert rail.updates == [("abc123", {"urls": [_CAROUSELL_ENTRY]})]


def test_a_mixed_set_pushed_twice_makes_one_call(store, bus) -> None:
    _item(store, {"carousell-ai": _RAIL_URL, "fb": _FB_URL, "craigslist": _CL_URL})
    rail = FakeRail()
    deps = _deps(store, bus, rail)

    crosslist.push_crosslinks(deps)
    crosslist.push_crosslinks(deps)
    assert rail.updates == [("abc123", {"urls": [_CL_ENTRY, _FB_ENTRY]})]


# --- a bazaar that has not learned Craigslist yet -----------------------------------------------

# What bazaar answers a set naming a platform it does not know: the names it does.
_UNKNOWN_PLATFORM = (
    "platform must be one of: EXTERNAL_PLATFORM_UNSPECIFIED, EXTERNAL_PLATFORM_CAROUSELL, "
    "EXTERNAL_PLATFORM_FACEBOOK_MARKETPLACE"
)


class RailWithoutCraigslist(FakeRail):
    """Refuses any set that names Craigslist, and counts the refusals."""

    def __init__(self):
        super().__init__()
        self.refused = 0

    def update_listing(self, listing_id, *, status=None, external_urls=None):
        if any(entry["platform"] == _CL_ENTRY["platform"] for entry in external_urls["urls"]):
            self.refused += 1
            raise RailToolRefused(_UNKNOWN_PLATFORM)
        return super().update_listing(listing_id, status=status, external_urls=external_urls)


class FailingRail(FakeRail):
    """Fails every update with one error, and counts the attempts."""

    def __init__(self, error):
        super().__init__()
        self.error = error
        self.calls = 0

    def update_listing(self, listing_id, *, status=None, external_urls=None):
        self.calls += 1
        raise self.error


def test_a_refused_craigslist_link_lets_the_other_links_sync(store, bus) -> None:
    item = _item(store, {"carousell-ai": _RAIL_URL, "fb": _FB_URL, "craigslist": _CL_URL})
    rail = RailWithoutCraigslist()
    deps = _deps(store, bus, rail)

    assert crosslist.push_crosslinks(deps) == 1
    assert rail.updates == [("abc123", {"urls": [_FB_ENTRY]})]
    assert _events(bus, "crosslink.push_failed") == []
    (refused,) = _events(bus, "crosslink.craigslist_refused")
    assert refused.payload == {"item_id": item["id"], "reason": _UNKNOWN_PLATFORM}

    store.archive_listing_url(item["id"], "fb")
    assert crosslist.push_crosslinks(deps) == 1
    assert rail.updates[-1] == ("abc123", {"urls": []})


def test_ticks_while_craigslist_is_refused_cost_no_calls_and_no_events(store, bus) -> None:
    _item(store, {"carousell-ai": _RAIL_URL, "fb": _FB_URL, "craigslist": _CL_URL})
    _item(store, {"carousell-ai": "https://www.carousell.ai/listing/def456", "craigslist": _CL_URL})
    rail = RailWithoutCraigslist()
    deps = _deps(store, bus, rail)

    for _ in range(5):
        crosslist.push_crosslinks(deps)

    assert rail.refused == 1  # one refusal holds every item's Craigslist link back
    assert rail.updates == [("abc123", {"urls": [_FB_ENTRY]})]
    assert len(_events(bus, "crosslink.craigslist_refused")) == 1
    assert len(_events(bus, "crosslink.pushed")) == 1
    assert _events(bus, "crosslink.push_failed") == []


def test_craigslist_is_offered_again_after_the_hold_and_goes_up_once_taken(store, bus) -> None:
    _item(store, {"carousell-ai": _RAIL_URL, "fb": _FB_URL, "craigslist": _CL_URL})
    clock = [1_000_000.0]
    rails = [RailWithoutCraigslist()]
    deps = crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=lambda: rails[0],
        now=lambda: clock[0],
    )
    crosslist.push_crosslinks(deps)

    clock[0] += crosslist.CRAIGSLIST_RETRY_SEC
    crosslist.push_crosslinks(deps)
    assert rails[0].refused == 2
    assert len(_events(bus, "crosslink.craigslist_refused")) == 1  # the same state, not news

    rails[0] = FakeRail()  # bazaar learns the platform
    crosslist.push_crosslinks(deps)
    assert rails[0].updates == []  # still inside the hold the last refusal began

    clock[0] += crosslist.CRAIGSLIST_RETRY_SEC
    assert crosslist.push_crosslinks(deps) == 1
    assert rails[0].updates == [("abc123", {"urls": [_CL_ENTRY, _FB_ENTRY]})]


@pytest.mark.parametrize(
    "error",
    [
        RailToolRefused(_UNKNOWN_PLATFORM + ", EXTERNAL_PLATFORM_CRAIGSLIST"),
        RailToolRefused("duplicate external url platform: EXTERNAL_PLATFORM_CRAIGSLIST"),
        RailToolRefused(
            "invalid url for platform EXTERNAL_PLATFORM_FACEBOOK_MARKETPLACE: "
            "url must use http or https"
        ),
        RailToolRefused("listing not found (code: not_found)"),
        RailNetworkError("rail unreachable: timeout"),
    ],
)
def test_any_other_failure_of_a_set_with_craigslist_is_still_reported(store, bus, error) -> None:
    item = _item(store, {"carousell-ai": _RAIL_URL, "fb": _FB_URL, "craigslist": _CL_URL})
    rail = FailingRail(error)

    assert crosslist.push_crosslinks(_deps(store, bus, rail)) == 0
    assert rail.calls == 1  # no second try without Craigslist
    (failed,) = _events(bus, "crosslink.push_failed")
    assert failed.payload == {"item_id": item["id"], "reason": str(error)}
    assert _events(bus, "crosslink.craigslist_refused") == []
    assert store.crosslink_pushed_urls() == {}


# Craigslist links outside the pinned /view/d/ shape, none of which may ever be pushed: the manage
# page, and a regional .html address that is the post's public page in a shape we do not pin.
_NOT_POST_LINKS = [_CL_MANAGE, "https://sfbay.craigslist.org/sfc/fuo/d/teak-lamp/7712345678.html"]
_MARKETS = ["carousell-ai", "carousell", "fb", "craigslist", "mercari"]
_URLS = st.sampled_from(
    ["", _RAIL_URL, _CAROUSELL_URL, _FB_URL, _CL_URL, _CL_REPOST, "https://www.mercari.com/item/1"]
    + _NOT_POST_LINKS
)


@_PROPERTY
@given(listing_urls=st.dictionaries(st.sampled_from(_MARKETS), _URLS))
def test_the_set_only_holds_recorded_public_links_one_per_platform(listing_urls) -> None:
    desired = crosslist.desired_external_urls(listing_urls)
    market_of = {platform: market for market, platform in crosslist.MARKET_PLATFORMS.items()}

    assert [entry["platform"] for entry in desired] == sorted({e["platform"] for e in desired})
    for entry in desired:
        assert listing_urls[market_of[entry["platform"]]] == entry["url"]
        if entry["platform"] == "EXTERNAL_PLATFORM_CRAIGSLIST":
            assert entry["url"] not in _NOT_POST_LINKS
