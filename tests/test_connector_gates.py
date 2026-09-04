"""Removing a marketplace stops work already queued against it, not only work not yet started.

Durable rows written before the removal — a pending sign-in, an owed survey, an accepted listing,
a queued publish, a composed reply — would each otherwise drive a market the seller has switched
off. Every consumer reads the setting at its own decision point; this file is one case per
consumer, and what is left behind must be resumable, since a market turned back on has to resume.
"""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

from sellee import passes, settings
from sellee.browser import adopt, connect, inbox, survey
from sellee.browser import markets as market_adapters
from sellee.channel import fastpaths
from sellee.config import Config
from sellee.store import CONNECT_MODE_OPEN
from sellee.store.survey import LISTING_ACCEPTED
from sellee.tools.registry import dispatch

_MARKET = "carousell"
_LISTINGS_URL = "https://www.carousell.sg/manage-listings/"


@pytest.fixture(autouse=True)
def _region(store):
    store.set_seller_config_section("basics", {"region": "SG"})


def _disconnect(store):
    """The seller taps remove."""
    seed_setting(store, "connected_markets", [])


class StubClient:
    """A browser that fails the test if a lane reaches it at all — asserting the absence, not just
    leaving it, is what stops a gate that merely reorders work from passing."""

    def __init__(self):
        self.navigations: list = []

    class _Exclusive:
        def __init__(self, client):
            self.client = client

        def __enter__(self):
            return self.client

        def __exit__(self, *exc):
            return False

    def exclusive(self):
        return self._Exclusive(self)

    def navigate_visible(self, url):
        """A read brings the tab forward first; for a stub that is just a navigation."""
        self.navigate(url)

    def navigate(self, url):
        raise AssertionError(f"a disconnected market was navigated: {url}")

    def ensure_frontmost(self, url):
        raise AssertionError(f"a disconnected market took the seller's window: {url}")

    def evaluate(self, function, **kwargs):
        raise AssertionError("a disconnected market was read")


# --- the read lane ------------------------------------------------------------------------------


def test_the_read_lane_skips_a_disconnected_market_entirely(store, bus) -> None:
    """Never opens the page, not merely "reads nothing" — a probe is what produces the logged-out
    notice."""
    _disconnect(store)
    deps = inbox.InboxDeps(store=store, bus=bus, config=Config(), browser_factory=StubClient)

    inbox.inbox_lane(deps)

    assert [e.kind for e in bus.store.read() if e.kind.startswith("browser.")] == []
    assert store.list_queued_notices() == []


# --- the sign-in lane ---------------------------------------------------------------------------


def test_a_pending_sign_in_for_a_disconnected_market_is_dropped(store, bus) -> None:
    """The market can be disconnected after the tap; the row is cleared rather than left to fire on
    an unrelated reconnect."""
    store.request_market_connect(_MARKET, CONNECT_MODE_OPEN)
    _disconnect(store)
    deps = connect.ConnectDeps(store=store, bus=bus, config=Config(), browser_factory=StubClient)

    connect.connect_lane(deps)

    assert store.pending_market_connects() == []


def test_a_stale_sign_in_button_says_the_market_is_off_rather_than_unknown(store, bus) -> None:
    """A stale sign-in button must not claim the market is unknown — one tap brings it back."""
    _disconnect(store)
    text, _controls = fastpaths.handle_fast_path(
        store,
        bus,
        {"kind": "action", "payload": {"choice": fastpaths.CB_CONNECT_MARKET, "ref": _MARKET}},
    )

    assert "isn't connected" in text
    assert store.pending_market_connects() == []


# --- the survey and the adoption ----------------------------------------------------------------


def test_an_owed_survey_is_left_owed_rather_than_served_or_abandoned(store, bus) -> None:
    """Reconnecting is a later tick that can serve it, so the question stays waiting."""
    store.request_market_survey(_MARKET)
    _disconnect(store)

    survey.discover_phase(
        survey.SurveyDeps(store=store, bus=bus, config=Config(), browser_factory=StubClient)
    )

    assert store.get_market_survey(_MARKET)["state"] == "due"


def test_an_accepted_listing_is_not_adopted_and_costs_no_attempt(store, bus) -> None:
    """Nothing about this listing went wrong, so it must not spend one of its three attempts."""
    store.record_survey_result(
        _MARKET,
        [
            {
                "listing_id": "111",
                "url": "https://www.carousell.sg/p/lamp-111/",
                "title": "Lamp",
                "price": 80.0,
                "price_text": "S$80",
            }
        ],
    )
    store.decide_discovered_listings(_MARKET, decision="manage", manage="relist")
    _disconnect(store)

    adopt.adopt_phase(
        survey.SurveyDeps(store=store, bus=bus, config=Config(), browser_factory=StubClient)
    )

    row = store.list_discovered_listings(_MARKET)[0]
    assert row["status"] == LISTING_ACCEPTED
    assert row["attempts"] == 0


# --- the publish pass ---------------------------------------------------------------------------


def test_a_queued_publish_for_a_disconnected_market_is_refused(store) -> None:
    """The runner and every enqueue door share this validator, so a publish queued while the market
    was on is covered after it goes off."""
    item = store.create_item(title="Teak lamp", list_price=80.0)
    _disconnect(store)

    with pytest.raises(passes.PassPayloadError, match="isn't connected"):
        passes.validate_payload("publish", {"item_id": item["id"], "market": _MARKET}, store)


def test_a_publish_to_the_rail_is_never_gated(store) -> None:
    """carousell.ai is where every listing goes, connected or not, and must never be refused by
    this list."""
    item = store.create_item(title="Teak lamp", list_price=80.0)
    _disconnect(store)

    passes.validate_payload("publish", {"item_id": item["id"]}, store)


# --- the reply ----------------------------------------------------------------------------------


def _sell_thread(store, market=_MARKET):
    item = store.create_item(title="Teak lamp", list_price=80.0)
    store.create_thread(
        thread_id=f"{market}:1",
        side="sell",
        market=market,
        counterpart_handle="bob",
        item_id=item["id"],
    )


def test_a_composed_reply_is_refused_with_nothing_recorded(make_ctx, store) -> None:
    """Refused before any reserve or intent, so no pacing slot is spent and the stale sweep has
    nothing to escalate."""
    _sell_thread(store)
    store.record_inbound(f"{_MARKET}:1", msg_id="m1", text="still there?", ts=100.0)
    _disconnect(store)
    ctx = make_ctx("attended")

    res = dispatch(
        "send_reply",
        {"thread_id": f"{_MARKET}:1", "text": "yes!", "in_msg_id": "m1"},
        ctx,
    )

    assert res["status"] == "not_connected"
    assert res["delivered"] == "no"
    assert store.unsettled_intents() == []


def test_reconnecting_lets_the_same_reply_through(make_ctx, store) -> None:
    """The gate is a switch, not a tombstone: the refusal must leave the thread answerable once
    the market is back on."""
    _sell_thread(store)
    store.record_inbound(f"{_MARKET}:1", msg_id="m1", text="still there?", ts=100.0)
    _disconnect(store)
    ctx = make_ctx("attended")
    dispatch("send_reply", {"thread_id": f"{_MARKET}:1", "text": "yes!", "in_msg_id": "m1"}, ctx)

    seed_setting(store, "connected_markets", [_MARKET])
    res = dispatch(
        "send_reply", {"thread_id": f"{_MARKET}:1", "text": "yes!", "in_msg_id": "m1"}, ctx
    )

    # No sink is wired here, so reaching the missing send path is the proof it got past the gates.
    assert res["status"] == "no_send_path"


def test_a_buy_thread_is_not_governed_by_the_sell_side_switch(make_ctx, store) -> None:
    """`connected_markets` governs where we sell for the seller; a buy thread is them approaching
    someone else's listing."""
    want = store.create_want(query="thing")
    store.create_thread(
        thread_id="cl:9", side="buy", market="cl", counterpart_handle="s", want_id=want["want_id"]
    )
    _disconnect(store)
    ctx = make_ctx("attended")

    res = dispatch("send_reply", {"thread_id": "cl:9", "text": "still available?"}, ctx)

    assert res["status"] == "no_send_path"  # reached the send path, not the connection gate


# --- the Connections block on /sellee -------------------------------------------------------------


def _card(store, bus):
    return fastpaths.handle_fast_path(store, bus, {"kind": "command", "text": "/sellee"})


def test_the_card_lists_a_market_that_is_off_so_it_can_be_found(store, bus) -> None:
    """A switch you can only see once it is on is not a switch anyone finds."""
    _disconnect(store)

    text, controls = _card(store, bus)

    assert "Carousell — off" in text
    assert ("Connect Carousell", f"{_MARKET}:{fastpaths.CB_ADD_MARKET}") in controls


def test_a_connected_market_reads_as_on_and_offers_the_way_back(store, bus) -> None:
    seed_setting(store, "connected_markets", [_MARKET])

    text, controls = _card(store, bus)

    assert "Carousell — on" in text
    assert ("Disconnect Carousell", f"{_MARKET}:{fastpaths.CB_REMOVE_MARKET}") in controls


def test_connecting_from_the_card_applies_immediately_with_an_undo(store, bus) -> None:
    """An authenticated tap on the seller's own card is the approval the gate waits for, so it
    applies through `set_now` — parsed, checked, and in the ledger with a working Undo."""
    _disconnect(store)
    text, _controls = fastpaths.handle_fast_path(
        store,
        bus,
        {"kind": "action", "payload": {"choice": fastpaths.CB_ADD_MARKET, "ref": _MARKET}},
    )

    assert settings.connected_markets(store) == [_MARKET]
    # Connecting opens the sign-in too; leaving the seller to find that step would make the switch
    # appear to do nothing.
    assert "sign in" in text
    assert store.pending_market_connects()[0]["market"] == _MARKET
    applied = [c for c in store.list_pending_changes() if c["key"] == "connected_markets"]
    assert applied == [] or applied[0]["status"] == "applied"  # never left awaiting approval


def test_disconnecting_from_the_card_says_the_sign_in_survives(store, bus) -> None:
    """The cookies are untouched, so the switch reads as reversible rather than destructive."""
    seed_setting(store, "connected_markets", [_MARKET])
    text, _controls = fastpaths.handle_fast_path(
        store,
        bus,
        {"kind": "action", "payload": {"choice": fastpaths.CB_REMOVE_MARKET, "ref": _MARKET}},
    )

    assert settings.connected_markets(store) == []
    assert "no password" in text


@pytest.mark.parametrize(
    "choice_attr,seeded,expected",
    [
        ("CB_ADD_MARKET", [_MARKET], "already on"),
        ("CB_REMOVE_MARKET", [], "already off"),
    ],
)
def test_a_stale_card_button_re_acks_rather_than_flipping(
    store, bus, choice_attr, seeded, expected
) -> None:
    """These buttons live in the scrollback forever, so a stale tap must never toggle — a stale
    Connect flipping a market off would be the opposite of what was pressed."""
    seed_setting(store, "connected_markets", seeded)
    text, _controls = fastpaths.handle_fast_path(
        store,
        bus,
        {"kind": "action", "payload": {"choice": getattr(fastpaths, choice_attr), "ref": _MARKET}},
    )

    assert expected in text
    assert settings.connected_markets(store) == seeded


def test_a_market_with_no_site_for_this_seller_is_refused_with_the_reason(store, bus) -> None:
    """`check_for_seller` still runs: a US seller cannot be connected to Carousell, which runs no
    US site."""
    store.set_seller_config_section("basics", {"region": "US"})
    _disconnect(store)
    text, _controls = fastpaths.handle_fast_path(
        store,
        bus,
        {"kind": "action", "payload": {"choice": fastpaths.CB_ADD_MARKET, "ref": _MARKET}},
    )

    assert settings.connected_markets(store) == []
    assert "US" in text or "can't work" in text


# --- what the setting still means ---------------------------------------------------------------


def test_connected_is_the_sellers_intent_and_publishable_is_the_narrower_question(store) -> None:
    """The two readers answer different questions: a market with no way to publish is still worked,
    but never listed to."""
    seed_setting(store, "connected_markets", [_MARKET, "mercari"])

    assert settings.connected_markets(store) == [_MARKET, "mercari"]
    assert settings.publish_markets(store) == [_MARKET]


def test_a_withdrawn_adapter_does_not_silently_vanish_from_the_sellers_list(
    store, monkeypatch
) -> None:
    """Intent is not filtered through capability: a withdrawn adapter must not make the market
    silently disappear from the seller's own list."""
    seed_setting(store, "connected_markets", [_MARKET])
    monkeypatch.setattr(market_adapters, "_ADAPTERS", {})

    assert settings.connected_markets(store) == [_MARKET]
    assert settings.publish_markets(store) == []
