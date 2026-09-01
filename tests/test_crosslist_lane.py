"""The fan-out lane: which items it queues a publish for, what it refuses to try twice, when it
holds off, and how each outcome reaches the seller.

The browser is a fake acquisition factory — these tests are about the lane's decisions. The
bring-up itself is covered in test_browser_client.py, and what acquiring does (Node check, Chrome
launch, the window notice) in test_browser_acquisition.py.
"""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

from sellee import crosslist, settings
from sellee.browser.client import BrowserUnavailable
from sellee.config import Config
from sellee.rail.client import RailUnprovisioned

_RAIL_URL = "https://www.carousell.ai/listing/abc123"
_CAROUSELL_URL = "https://www.carousell.sg/p/teak-lamp-1328307791/"


def _no_rail():
    raise RailUnprovisioned("carousell.ai is not provisioned")


def _deps(store, bus, browser_factory=None, rail_factory=None, **overrides):
    return crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(**overrides) if overrides else Config(),
        # The default posture: acquiring the browser succeeds without launching anything.
        browser_factory=browser_factory if browser_factory is not None else lambda: object(),
        # And no rail key, so the push phase skips — these tests are about the lane's decisions.
        rail_factory=rail_factory if rail_factory is not None else _no_rail,
        # Real time, because the retry cooldown compares this clock against the store-written
        # `finished_ts`; a fixed fake hour quietly turns the cooldown off. Tests about a specific
        # hour pass their own.
    )


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


@pytest.fixture
def enabled(store):
    """A seller in SG who has asked for Carousell, one item live on the rail, and past the
    look-before-listing gate: the fan-out will not publish to a marketplace it has never read."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    store.record_survey_result("carousell", [])
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell-ai", _RAIL_URL)
    return store.get_item(item["id"])


def _queued(store):
    # Projected: `finished_ts` is the retry clock's business, not these tests'.
    return [
        {k: v for k, v in row.items() if k != "finished_ts"}
        for row in store.publish_pass_index()
        if row["status"] == "queued"
    ]


def _notices(store):
    return [n["text"] for n in store.claim_queued_notices(10)]


# --- what gets queued -------------------------------------------------------------------------


def test_a_rail_listed_item_is_queued_for_the_enabled_market(store, bus, enabled) -> None:
    pass_id = crosslist.enqueue_next(_deps(store, bus))

    assert pass_id
    assert _queued(store) == [
        {
            "market": "carousell",
            "item_id": enabled["id"],
            "origin": "crosslist",
            "status": "queued",
        }
    ]


def test_an_item_not_on_the_rail_yet_is_not_queued(store, bus) -> None:
    """Rail-first is a precondition, not an instruction: with no carousell.ai listing there is
    nothing to fan out from."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    store.create_item(title="Teak lamp", list_price=80.0)

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_an_item_already_on_the_market_is_not_queued(store, bus, enabled) -> None:
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)
    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_nothing_is_queued_with_the_setting_at_its_default(store, bus) -> None:
    # Back to the registry default — the shared store fixture seeds a connected market.
    seed_setting(store, "connected_markets", [])
    store.set_seller_config_section("basics", {"region": "SG"})
    item = store.create_item(title="Teak lamp", list_price=80.0)
    store.record_listing_url(item["id"], "carousell-ai", _RAIL_URL)

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_a_sold_item_is_not_queued(store, bus, enabled) -> None:
    """Its rail listing is about to be archived and its other listings are take-down work, so
    starting a new one would create something to immediately close."""
    store.create_thread(
        thread_id="carousell:1",
        side="sell",
        market="carousell",
        counterpart_handle="buyer",
        item_id=enabled["id"],
    )
    store.negotiate_confirm_sold(enabled["id"], "carousell:1")

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_a_market_the_sellers_region_lost_is_not_queued(store, bus, enabled) -> None:
    """The stored value still names Carousell, but a US account has nowhere to be listed there."""
    store.set_seller_config_section("basics", {"region": "US"})
    assert crosslist.enqueue_next(_deps(store, bus)) is None


# --- one shot ---------------------------------------------------------------------------------


def _later(seconds: float):
    """A clock past the retry cooldown, so a test can reach the next attempt without waiting."""
    import time as _time

    stamp = _time.time() + seconds
    return lambda: stamp


@pytest.mark.parametrize("status,cls", [("done", "ok"), ("error", "error"), ("error", "timeout")])
def test_a_settled_attempt_is_not_retried_immediately(store, bus, enabled, status, cls) -> None:
    """Retrying on the next tick is not a retry — nothing has had time to change."""
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.finish_pass(pass_id, status=status, rc=1, cls=cls, summary=cls)

    assert crosslist.enqueue_next(_deps(store, bus)) is None


@pytest.mark.parametrize("status,cls", [("done", "ok"), ("error", "error"), ("error", "timeout")])
def test_a_publish_that_left_no_listing_is_retried_later(store, bus, enabled, status, cls) -> None:
    """A listing that did not appear is not something a seller should have to notice and ask for;
    `done` with no recorded URL counts too."""
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.finish_pass(pass_id, status=status, rc=1, cls=cls, summary=cls)

    later = _deps(store, bus)
    later.now = _later(crosslist.PUBLISH_RETRY_AFTER_SEC + 60)

    assert crosslist.enqueue_next(later)


def test_retries_run_out(store, bus, enabled) -> None:
    """Bounded, because the failure is often one no retry can fix."""
    for _ in range(crosslist.PUBLISH_MAX_ATTEMPTS):
        deps = _deps(store, bus)
        deps.now = _later(crosslist.PUBLISH_RETRY_AFTER_SEC * 100)
        pass_id = crosslist.enqueue_next(deps)
        assert pass_id, "gave up before the bound"
        store.finish_pass(pass_id, status="error", rc=1, cls="error", summary="error")

    deps = _deps(store, bus)
    deps.now = _later(crosslist.PUBLISH_RETRY_AFTER_SEC * 100)
    assert crosslist.enqueue_next(deps) is None


def test_an_attempt_by_hand_also_counts(store, bus, enabled) -> None:
    """Same pair, same ledger: the lane's memory is the pass history, not a counter of its own."""
    manual = store.enqueue_pass("publish", {"item_id": enabled["id"], "market": "carousell"})
    store.finish_pass(manual, status="error", rc=1, cls="error", summary="error")

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_a_rail_publish_never_counts_as_an_attempt(store, bus, enabled) -> None:
    rail = store.enqueue_pass("publish", {"item_id": enabled["id"], "market": "carousell-ai"})
    store.finish_pass(rail, status="done", rc=0, cls="ok", summary="ok")

    assert crosslist.enqueue_next(_deps(store, bus))


def test_only_one_publish_is_in_flight_at_a_time(store, bus, enabled) -> None:
    other = store.create_item(title="Brass lamp", list_price=40.0)
    store.record_listing_url(other["id"], "carousell-ai", _RAIL_URL + "2")

    assert crosslist.enqueue_next(_deps(store, bus))
    assert crosslist.enqueue_next(_deps(store, bus)) is None  # one is already queued
    assert len(_queued(store)) == 1


# --- when it holds off ------------------------------------------------------------------------


def test_a_paused_agent_queues_nothing(store, bus, enabled) -> None:
    store.set_paused(True)
    crosslist.crosslist_lane(_deps(store, bus))
    assert _queued(store) == []


def test_listing_is_not_held_by_quiet_hours(store, bus, enabled) -> None:
    """A listing sits there until someone looks at it, so the hour it went up is not something a
    buyer sees — unlike a nudge, which is still held."""
    seed_setting(store, "quiet_hours", [2300, 800])

    deps = crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=_no_rail,
        now=_midnight,
    )
    crosslist.crosslist_lane(deps)
    assert len(_queued(store)) == 1

    deps = crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=_no_rail,
        now=_noon,
    )
    crosslist.crosslist_lane(deps)
    assert len(_queued(store)) == 1


# --- the browser has to be there --------------------------------------------------------------


def _unavailable_factory(reason):
    def _factory():
        raise BrowserUnavailable(reason)

    return _factory


def test_an_unavailable_browser_means_no_attempt_spent(store, bus, enabled) -> None:
    deps = _deps(store, bus, browser_factory=_unavailable_factory("'npx' is not installed"))
    assert crosslist.enqueue_next(deps) is None
    notices = _notices(store)
    assert any("can't drive a browser" in text for text in notices)
    assert any("npx" in text for text in notices)  # the acquisition's reason, verbatim

    # Eligibility survives, so fixing the environment is all it takes.
    assert crosslist.enqueue_next(_deps(store, bus))


def test_chrome_that_will_not_start_means_no_attempt_spent(store, bus, enabled) -> None:
    """A failed bring-up surfaces as the acquisition raising with the by-hand launch command."""
    hint = (
        "Chrome is not running on port 9222 — start it with:\n"
        "  /bin/chrome --remote-debugging-port=9222"
    )
    deps = _deps(store, bus, browser_factory=_unavailable_factory(hint))
    assert crosslist.enqueue_next(deps) is None
    assert any("--remote-debugging-port" in text for text in _notices(store))

    assert crosslist.enqueue_next(_deps(store, bus))


def test_a_working_browser_is_not_announced_by_the_lane(store, bus, enabled) -> None:
    """The window notice belongs to the acquisition (which knows whether a launch happened), not
    to this lane — a clean acquisition queues nothing."""
    crosslist.enqueue_next(_deps(store, bus))
    assert _notices(store) == []


def test_a_repeated_failure_tells_the_seller_once(store, bus, enabled) -> None:
    deps = _deps(store, bus, browser_factory=_unavailable_factory("'npx' is not installed"))
    for _ in range(3):
        crosslist.enqueue_next(deps)

    assert len(_notices(store)) == 1


# --- reporting the outcome --------------------------------------------------------------------


def test_a_recorded_url_is_reported_as_a_live_listing(store, bus, enabled) -> None:
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)
    store.finish_pass(pass_id, status="done", rc=0, cls="ok", summary="ok")

    assert crosslist.report_settled(_deps(store, bus)) == 1
    notices = _notices(store)
    assert notices == [f"Teak lamp is now listed on Carousell: {_CAROUSELL_URL}"]


def _burn_attempts(store, bus, status="done", cls="ok"):
    """Run the pair out of attempts, settling each one without a listing URL."""
    for _ in range(crosslist.PUBLISH_MAX_ATTEMPTS):
        deps = _deps(store, bus)
        deps.now = _later(crosslist.PUBLISH_RETRY_AFTER_SEC * 100)
        pass_id = crosslist.enqueue_next(deps)
        assert pass_id
        store.finish_pass(pass_id, status=status, rc=0, cls=cls, summary=cls)


def test_a_failure_still_being_retried_is_not_announced(store, bus, enabled) -> None:
    """Repeated "couldn't list" notices for a desk about to appear train the seller to ignore the
    one that matters."""
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.finish_pass(pass_id, status="error", rc=1, cls="error", summary="error")

    assert crosslist.report_settled(_deps(store, bus)) == 0
    assert _notices(store) == []


def test_a_clean_exit_without_a_url_is_reported_once_the_retries_run_out(store, bus, enabled):
    """The 07 shape: the pass said it was done and recorded nothing, so no listing exists that
    anyone can find. The row is the fact, not the exit code."""
    _burn_attempts(store, bus)

    assert crosslist.report_settled(_deps(store, bus)) >= 1
    text = [t for t in _notices(store) if "couldn't list" in t][0]
    assert "couldn't list Teak lamp on Carousell" in text
    # The retry is something to ask for, not a command to run: the notice lands on a phone.
    assert "Ask me" in text


def test_a_failure_names_the_retry_and_reassures_about_the_rail(store, bus, enabled) -> None:
    _burn_attempts(store, bus, status="error", cls="error")

    crosslist.report_settled(_deps(store, bus))
    assert any("carousell.ai listing" in t for t in _notices(store))


def test_a_running_publish_is_not_reported_yet(store, bus, enabled) -> None:
    crosslist.enqueue_next(_deps(store, bus))
    assert crosslist.report_settled(_deps(store, bus)) == 0
    assert _notices(store) == []


def test_an_outcome_is_reported_exactly_once(store, bus, enabled) -> None:
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)
    store.finish_pass(pass_id, status="done", rc=0, cls="ok", summary="ok")

    deps = _deps(store, bus)
    assert crosslist.report_settled(deps) == 1
    assert crosslist.report_settled(deps) == 0
    assert len(_notices(store)) == 1


def test_a_publish_run_by_hand_is_not_reported(store, bus, enabled) -> None:
    """Whoever ran it is watching it; a notice would be the daemon narrating their own command."""
    manual = store.enqueue_pass("publish", {"item_id": enabled["id"], "market": "carousell"})
    store.finish_pass(manual, status="done", rc=0, cls="ok", summary="ok")

    assert crosslist.report_settled(_deps(store, bus)) == 0
    assert _notices(store) == []


def test_reporting_runs_even_while_paused(store, bus, enabled) -> None:
    """A pause stops the agent acting, not telling the seller what already happened."""
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)
    store.finish_pass(pass_id, status="done", rc=0, cls="ok", summary="ok")
    store.set_paused(True)

    crosslist.crosslist_lane(_deps(store, bus))
    assert len(_notices(store)) == 1


# --- backfill ---------------------------------------------------------------------------------


def test_enabling_a_market_picks_up_items_listed_before(store, bus) -> None:
    """Nothing special-cases the backlog: eligibility is a query, so items published long before the
    setting existed qualify the moment it is turned on."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", [])  # not turned on yet — that is the point here
    old = store.create_item(title="Teak lamp", list_price=80.0)
    store.record_listing_url(old["id"], "carousell-ai", _RAIL_URL)
    assert crosslist.enqueue_next(_deps(store, bus)) is None

    seed_setting(store, "connected_markets", ["carousell"])
    store.record_survey_result("carousell", [])  # looked at, and the seller had nothing there
    assert crosslist.enqueue_next(_deps(store, bus))


# --- what the seller conversation says about it ------------------------------------------------


def test_the_listing_flow_names_the_destinations_without_claiming_the_fan_out() -> None:
    """Listing something sets expectations and stops there — the fan-out is the daemon's, and a
    recipe that thought it had to trigger it would fire a publish per listing."""
    from sellee import skills

    recipe = skills.load("listing-flow")
    assert "connected_markets" in recipe
    assert "not your job" in recipe
    assert "background" in recipe
    assert "Never trigger it as part of listing something" in recipe


def test_the_listing_flow_points_a_retry_at_the_tool() -> None:
    """The other half: after a failure, asking is what restarts it, so the recipe has to know the
    tool exists — otherwise the model repeats the "nothing for me to trigger" line at a seller who
    is asking for exactly that."""
    from sellee import skills

    recipe = skills.load("listing-flow")
    assert "queue_marketplace_publish" in recipe


def test_the_channel_pass_can_see_the_setting_it_is_told_to_name(store, bus, enabled) -> None:
    """Naming the destinations requires knowing them: the settings block carries the value."""
    block = settings.prompt_block(store)
    assert "connected_markets" in block
    assert "Carousell" in block


def test_settings_read_filters_to_publishable_markets(store, bus, enabled) -> None:
    """A stale id in the stored value is not an eligible publish."""
    seed_setting(store, "connected_markets", ["carousell", "mercari"])
    assert settings.publish_markets(store) == ["carousell"]
    assert [market for _, market in crosslist.pending_pairs(_deps(store, bus))] == ["carousell"]


# --- looking before listing ----------------------------------------------------------------------


def _rail_item(store, title="Teak lamp"):
    item = store.create_item(title=title, list_price=80.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell-ai", _RAIL_URL)
    return store.get_item(item["id"])


def test_nothing_is_published_to_a_marketplace_we_have_never_read(store, bus) -> None:
    """Fanning out before reading the marketplace would repost listings the seller already put
    there by hand, which nothing in the store knows about."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    _rail_item(store)

    assert crosslist.pending_pairs(_deps(store, bus)) == []
    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_the_lane_asks_for_the_look_it_is_waiting_on(store, bus) -> None:
    """The lane asks for the survey itself; the request is insert-only, so it is a no-op once one
    exists."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    _rail_item(store)

    crosslist.pending_pairs(_deps(store, bus))

    assert store.get_market_survey("carousell")["state"] == "due"


def test_once_the_look_is_done_the_fan_out_proceeds(store, bus) -> None:
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    item = _rail_item(store)
    store.record_survey_result("carousell", [])

    assert [i["id"] for i, _m in crosslist.pending_pairs(_deps(store, bus))] == [item["id"]]


def test_an_item_the_seller_already_has_there_is_not_listed_again(store, bus) -> None:
    """Matched against what the survey *found*, not against what we manage — see below."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    _rail_item(store, title="Teak lamp")
    store.record_survey_result(
        "carousell",
        [
            {
                "listing_id": "1",
                "url": "https://www.carousell.sg/p/lamp-1/",
                "title": "Teak Lamp",
                "price": 80.0,
                "price_text": "S$80",
            }
        ],
    )

    assert crosslist.pending_pairs(_deps(store, bus)) == []


def test_a_listing_the_seller_declined_to_manage_still_stops_a_second_copy(store, bus) -> None:
    """A declined listing is still on their marketplace while the item looks absent from it, so the
    fan-out must not post a second copy."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    _rail_item(store, title="Teak lamp")
    store.record_survey_result(
        "carousell",
        [
            {
                "listing_id": "1",
                "url": "https://www.carousell.sg/p/lamp-1/",
                "title": "Teak lamp",
                "price": 80.0,
                "price_text": "S$80",
            }
        ],
    )
    store.decide_discovered_listings("carousell", decision="decline")

    assert crosslist.pending_pairs(_deps(store, bus)) == []


def test_something_the_seller_does_not_have_there_is_still_listed(store, bus) -> None:
    """The gate must not become a blanket refusal; an item missing from that marketplace is what
    the fan-out is for."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    item = _rail_item(store, title="Dyson HushJet Mini Cool Fan")
    store.record_survey_result(
        "carousell",
        [
            {
                "listing_id": "1",
                "url": "https://www.carousell.sg/p/lamp-1/",
                "title": "Teak lamp",
                "price": 80.0,
                "price_text": "S$80",
            }
        ],
    )
    store.decide_discovered_listings("carousell", decision="decline")  # nothing outstanding

    assert [i["id"] for i, _m in crosslist.pending_pairs(_deps(store, bus))] == [item["id"]]


def test_a_marketplace_nothing_can_survey_is_not_held_back(store, bus, monkeypatch) -> None:
    """Blocking on a look that can never come would mean never publishing there at all."""
    monkeypatch.setattr(crosslist.market_adapters, "can_survey", lambda market, region=None: False)
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    item = _rail_item(store)

    assert [i["id"] for i, _m in crosslist.pending_pairs(_deps(store, bus))] == [item["id"]]


def test_a_driven_publish_is_reported_to_the_seller(store, bus, enabled, monkeypatch) -> None:
    """A driven market spawns no pass; its ledger row must still be reported, or a listing goes
    live with nobody told."""
    pass_id = store.record_driven_publish(
        enabled["id"], "carousell", status="done", origin=crosslist.ORIGIN
    )
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)

    assert crosslist.report_settled(_deps(store, bus)) == 1
    assert any(_CAROUSELL_URL in text for text in _notices(store))
    assert crosslist.report_settled(_deps(store, bus)) == 0  # exactly once
    assert pass_id


def test_a_driven_publish_that_recorded_no_url_is_reported_once_it_gives_up(
    store, bus, enabled
) -> None:
    for _ in range(crosslist.PUBLISH_MAX_ATTEMPTS):
        store.record_driven_publish(
            enabled["id"], "carousell", status="error", origin=crosslist.ORIGIN
        )

    assert crosslist.report_settled(_deps(store, bus)) >= 1
    assert any("couldn't list" in text for text in _notices(store))


def test_an_abandoned_survey_does_not_open_the_gate(store, bus) -> None:
    """An abandoned survey means we know less than when we started; treating it as "looked" opens
    the gate at the worst moment."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    _rail_item(store)
    store.abandon_market_survey("carousell")

    assert crosslist.pending_pairs(_deps(store, bus)) == []


def test_the_fan_out_waits_while_the_seller_is_still_being_asked(store, bus) -> None:
    """The title match is whole-string, so a differently worded listing is only caught by the ask;
    publishing first posts the copy the ask exists to prevent."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    _rail_item(store, title="If Anyone Builds It, Everyone Dies by Yudkowsky & Soares")
    store.record_survey_result(
        "carousell",
        [
            {
                "listing_id": "1",
                "url": "https://www.carousell.sg/p/book-1/",
                "title": "If Anyone Builds It, Everyone Dies (Yudkowsky & Soares)",
                "price": 20.0,
                "price_text": "S$20",
            }
        ],
    )

    assert crosslist.pending_pairs(_deps(store, bus)) == []


def test_answering_the_ask_releases_the_fan_out(store, bus) -> None:
    """The hold is temporary: once nothing is outstanding the lane proceeds."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    item = _rail_item(store, title="Dyson HushJet Mini Cool Fan")
    store.record_survey_result(
        "carousell",
        [
            {
                "listing_id": "1",
                "url": "https://www.carousell.sg/p/lamp-1/",
                "title": "Teak lamp",
                "price": 80.0,
                "price_text": "S$80",
            }
        ],
    )
    assert crosslist.pending_pairs(_deps(store, bus)) == []  # held while unanswered

    store.decide_discovered_listings("carousell", decision="decline")

    assert [i["id"] for i, _m in crosslist.pending_pairs(_deps(store, bus))] == [item["id"]]


# --- driving a publish ourselves ----------------------------------------------------------------
#
# One invariant above all: nothing may leave `_drive_publish` without a ledger row unless the pair
# is genuinely still worth trying — the row is what stands between a failed publish and a
# duplicate one.


class _HeldClient:
    """The daemon's browser, as far as the fan-out is concerned: something it can hold."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exclusive(self):
        return self


def _driving(store, bus, monkeypatch, *, outcome=None, raises=None, published=None):
    """A seller with one item eligible for a driven market, and a publisher that answers
    to order."""
    from sellee.browser import publisher

    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    store.record_survey_result("carousell", [])
    item = _rail_item(store)

    monkeypatch.setattr(publisher, "can_drive", lambda market: True)
    monkeypatch.setattr(publisher, "stage_photos", lambda item_id, photos: [])
    monkeypatch.setattr(publisher, "clear_staged", lambda item_id: None)

    def fake_publish(*args, **kwargs):
        if raises is not None:
            raise raises
        if published is not None:
            published.append(True)
        return outcome

    monkeypatch.setattr(publisher, "publish", fake_publish)
    return item


def _ledger(store):
    return [r for r in store.publish_pass_index() if r["origin"] == "crosslist"]


def test_a_terminal_refusal_spends_the_shot_rather_than_looping(store, bus, monkeypatch) -> None:
    """The lane always takes the first eligible pair, so a refusal that can never succeed would
    re-drive the same item forever."""
    from sellee.browser import publisher

    item = _driving(
        store,
        bus,
        monkeypatch,
        raises=publisher.PublishNotAttempted("the form shows the title as truncated"),
    )
    deps = _deps(store, bus, browser_factory=_HeldClient)

    crosslist.enqueue_next(deps)

    assert len(_ledger(store)) == 1, "no row was written, so the pair stays eligible forever"
    assert crosslist.pending_pairs(deps) == []
    assert item


def test_a_transient_refusal_is_retried_but_not_forever(store, bus, monkeypatch) -> None:
    """ "Retryable" with no bound is the same forever-loop by another name."""
    from sellee.browser import publisher

    _driving(
        store,
        bus,
        monkeypatch,
        raises=publisher.PublishNotAttempted("could not fill title", retryable=True),
    )
    deps = _deps(store, bus, browser_factory=_HeldClient)

    for _ in range(crosslist.MAX_DRIVE_ATTEMPTS - 1):
        crosslist.enqueue_next(deps)
        assert _ledger(store) == [], "gave up too early on a genuinely transient failure"
        assert crosslist.pending_pairs(deps), "the pair should still be eligible"

    crosslist.enqueue_next(deps)

    assert len(_ledger(store)) == 1
    assert crosslist.pending_pairs(deps) == []


def test_an_unverified_publish_is_never_driven_twice(store, bus, monkeypatch) -> None:
    from sellee.browser import publisher

    _driving(
        store,
        bus,
        monkeypatch,
        raises=publisher.PublishUnverified("the publish may have gone through"),
    )
    deps = _deps(store, bus, browser_factory=_HeldClient)

    crosslist.enqueue_next(deps)

    assert len(_ledger(store)) == 1
    assert crosslist.pending_pairs(deps) == []


def test_an_unexpected_browser_error_is_treated_as_maybe_published(store, bus, monkeypatch) -> None:
    """A bare error escaping the driver could come from either side of the commit, so it is treated
    as the dangerous side."""
    from sellee.browser.client import BrowserToolError

    _driving(store, bus, monkeypatch, raises=BrowserToolError("chrome went away"))
    deps = _deps(store, bus, browser_factory=_HeldClient)

    crosslist.enqueue_next(deps)

    assert len(_ledger(store)) == 1
    assert crosslist.pending_pairs(deps) == []


def test_a_verified_publish_records_the_url_and_reports_it(store, bus, monkeypatch) -> None:
    from sellee.browser.publisher import PublishOutcome

    item = _driving(
        store,
        bus,
        monkeypatch,
        outcome=PublishOutcome(listing_id="9", url=_CAROUSELL_URL, verified=True),
    )
    deps = _deps(store, bus, browser_factory=_HeldClient)

    crosslist.enqueue_next(deps)

    assert store.get_item(item["id"])["listing_urls"]["carousell"] == _CAROUSELL_URL
    assert crosslist.report_settled(deps) == 1
    assert any(_CAROUSELL_URL in text for text in _notices(store))


def test_a_driven_publish_never_runs_while_a_pair_is_ineligible(store, bus, monkeypatch) -> None:
    """The guard above the driver: nothing is driven for a market that has not been looked at."""
    from sellee.browser import publisher

    published: list = []
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    _rail_item(store)  # deliberately NO survey result
    monkeypatch.setattr(publisher, "can_drive", lambda market: True)
    monkeypatch.setattr(publisher, "publish", lambda *a, **k: published.append(True))

    crosslist.enqueue_next(_deps(store, bus, browser_factory=_HeldClient))

    assert published == []
