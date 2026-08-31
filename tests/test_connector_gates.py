"""Removing a marketplace stops work already queued against it, not only work not yet started.

`connected_markets` is the seller's switch for a marketplace, and the promise it makes is that
turning it off stops the agent touching that market. That promise is only as good as its weakest
consumer: durable rows written before the removal — a pending sign-in, an owed survey, an accepted
listing, a queued publish, a composed reply — would each otherwise drive a market the seller has
just switched off, because each of them was authorised at a different moment.

So every one of them reads the setting at its own decision point, and this file is one case per
consumer: arrange the work, remove the market, and assert nothing happens. The paired assertion is
about *what is left behind* — a market a seller can turn back on has to resume, so a gate that
threw the work away would be its own kind of broken.
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
    """The seller taps remove. The one thing every test here does between arranging and acting."""
    seed_setting(store, "connected_markets", [])


class StubClient:
    """A browser that fails the test if a lane reaches it at all.

    Every case below has been disconnected, so no lane has any business navigating: making that an
    assertion rather than an absence is what stops a gate that merely reorders work from passing.
    """

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

    def navigate(self, url):
        raise AssertionError(f"a disconnected market was navigated: {url}")

    def ensure_frontmost(self, url):
        raise AssertionError(f"a disconnected market took the seller's window: {url}")

    def evaluate(self, function, **kwargs):
        raise AssertionError("a disconnected market was read")


# --- the read lane ------------------------------------------------------------------------------


def test_the_read_lane_skips_a_disconnected_market_entirely(store, bus) -> None:
    """Not merely "reads nothing" — never opens the page. A probe is what produces the logged-out
    notice, so a lane that navigated first and gated later would still be telling a seller to sign
    in to a marketplace they have switched off."""
    _disconnect(store)
    deps = inbox.InboxDeps(store=store, bus=bus, config=Config(), browser_factory=StubClient)

    inbox.inbox_lane(deps)

    assert [e.kind for e in bus.store.read() if e.kind.startswith("browser.")] == []
    assert store.list_queued_notices() == []


# --- the sign-in lane ---------------------------------------------------------------------------


def test_a_pending_sign_in_for_a_disconnected_market_is_dropped(store, bus) -> None:
    """The row is durable and the tap that wrote it may be old, so the market can be disconnected in
    between. Cleared rather than left pending: nothing about waiting would make it servable, and a
    row that stayed would open a window the moment it was reconnected for an unrelated reason."""
    store.request_market_connect(_MARKET, CONNECT_MODE_OPEN)
    _disconnect(store)
    deps = connect.ConnectDeps(store=store, bus=bus, config=Config(), browser_factory=StubClient)

    connect.connect_lane(deps)

    assert store.pending_market_connects() == []


def test_a_stale_sign_in_button_says_the_market_is_off_rather_than_unknown(store, bus) -> None:
    """The buttons live forever, so this tap is really a question about when it was tapped. "I don't
    sell there" would be wrong about a marketplace one tap away from coming back."""
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
    """Owed, not abandoned. Abandoning is for "no later tick could ever serve this", and
    reconnecting is precisely a later tick that can — a seller who turns a market off and back on
    should find the question about their existing listings still waiting."""
    store.request_market_survey(_MARKET)
    _disconnect(store)

    survey.discover_phase(
        survey.SurveyDeps(store=store, bus=bus, config=Config(), browser_factory=StubClient)
    )

    assert store.get_market_survey(_MARKET)["state"] == "due"


def test_an_accepted_listing_is_not_adopted_and_costs_no_attempt(store, bus) -> None:
    """Nothing about this listing went wrong, so it must not spend one of its three attempts —
    three ticks with the market off would otherwise retire a perfectly good row for good."""
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
    """The runner and every enqueue door share this validator, so gating here covers a publish that
    was queued while the market was on and reaches the runner after it went off."""
    item = store.create_item(title="Teak lamp", list_price=80.0)
    _disconnect(store)

    with pytest.raises(passes.PassPayloadError, match="isn't connected"):
        passes.validate_payload("publish", {"item_id": item["id"], "market": _MARKET}, store)


def test_a_publish_to_the_rail_is_never_gated(store) -> None:
    """carousell.ai is where every listing goes, connected or not — it is not a member of this list
    and must never be refused by it."""
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
    """The one outcome that would make the switch worthless is a message going out on the seller's
    account after they turned the market off. Refused beside the pause check, before any reserve or
    intent, so no pacing slot is spent and the stale sweep has nothing to escalate."""
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
    """The gate is a switch, not a tombstone: nothing about the refusal above may leave the thread
    unable to be answered once the seller turns the market back on."""
    _sell_thread(store)
    store.record_inbound(f"{_MARKET}:1", msg_id="m1", text="still there?", ts=100.0)
    _disconnect(store)
    ctx = make_ctx("attended")
    dispatch("send_reply", {"thread_id": f"{_MARKET}:1", "text": "yes!", "in_msg_id": "m1"}, ctx)

    seed_setting(store, "connected_markets", [_MARKET])
    res = dispatch(
        "send_reply", {"thread_id": f"{_MARKET}:1", "text": "yes!", "in_msg_id": "m1"}, ctx
    )

    # No sink is wired on this context, so a reply that passes every gate lands on the send path
    # being absent — which is the proof it got past them.
    assert res["status"] == "no_send_path"


def test_a_buy_thread_is_not_governed_by_the_sell_side_switch(make_ctx, store) -> None:
    """`connected_markets` is the list of marketplaces we sell *for* the seller on. A buy thread is
    them approaching someone else's listing, which that switch has never governed and has no door to
    turn on — gating it here would stop buying on a setting that never claimed to be about it."""
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
    """A switch you can only see once it is on is not a switch anyone finds. The block is built
    from every marketplace we *could* work, so one that is off is still listed, with the button
    that turns it on."""
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
    """The approval gate on this setting exists to stop the *model* switching a marketplace on
    unasked. An authenticated tap on the seller's own card has already given that signal, so it
    applies through `set_now` — which still parses, still checks seller state, and still records the
    prior value so the change is in the ledger with a working Undo."""
    _disconnect(store)
    text, _controls = fastpaths.handle_fast_path(
        store,
        bus,
        {"kind": "action", "payload": {"choice": fastpaths.CB_ADD_MARKET, "ref": _MARKET}},
    )

    assert settings.connected_markets(store) == [_MARKET]
    # Connecting opens the sign-in too: until the seller is signed in there is nothing to read, so
    # a switch that left them to find the second step themselves would appear to do nothing.
    assert "sign in" in text
    assert store.pending_market_connects()[0]["market"] == _MARKET
    applied = [c for c in store.list_pending_changes() if c["key"] == "connected_markets"]
    assert applied == [] or applied[0]["status"] == "applied"  # never left awaiting approval


def test_disconnecting_from_the_card_says_the_sign_in_survives(store, bus) -> None:
    """The seller's cookies are untouched, and saying so is what makes the switch feel reversible
    rather than destructive — the difference between "off for now" and "start over"."""
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
    """These buttons live in the scrollback forever, so a tap that changes nothing is ordinary. It
    must never toggle: a stale Connect on an already-connected market that flipped it off would be
    the exact opposite of what the seller pressed."""
    seed_setting(store, "connected_markets", seeded)
    text, _controls = fastpaths.handle_fast_path(
        store,
        bus,
        {"kind": "action", "payload": {"choice": getattr(fastpaths, choice_attr), "ref": _MARKET}},
    )

    assert expected in text
    assert settings.connected_markets(store) == seeded


def test_a_market_with_no_site_for_this_seller_is_refused_with_the_reason(store, bus) -> None:
    """`check_for_seller` still runs on this door. A US seller cannot be connected to Carousell,
    which runs no US site, and the refusal is written for them."""
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
    """The two readers answer different questions and must not collapse into one. A market with no
    publish recipe is worked — its inbox read, its buyers answered — but never listed to."""
    seed_setting(store, "connected_markets", [_MARKET, "fb"])

    assert settings.connected_markets(store) == [_MARKET, "fb"]
    assert settings.publish_markets(store) == [_MARKET]  # fb ships no recipe


def test_a_withdrawn_adapter_does_not_silently_vanish_from_the_sellers_list(
    store, monkeypatch
) -> None:
    """Intent is not filtered through current capability. Quietly dropping a market here would make
    it disappear from the seller's own list of connected marketplaces after a release withdrew an
    adapter, with nothing to tell them why — where the lane simply skipping it is legible."""
    seed_setting(store, "connected_markets", [_MARKET])
    monkeypatch.setattr(market_adapters, "_ADAPTERS", {})

    assert settings.connected_markets(store) == [_MARKET]
    assert settings.publish_markets(store) == []
