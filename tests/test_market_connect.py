"""Signing in to a marketplace from chat: the tap, the row, and the lane that serves it.

The split under test is the point of the design. A tap lands on the provider's receive loop, which
is answering every other message in the chat, so it may only write a row — the browser work
happens on a lane. These tests hold both halves to that: the fast paths never touch Chrome, and
the lane always ends in exactly one notice the seller can act on.
"""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

from sellee.browser import connect
from sellee.browser.markets import carousell as carousell_market
from sellee.channel import fastpaths
from sellee.config import Config
from sellee.store import CONNECT_MODE_OPEN, CONNECT_MODE_PROBE

_HOME = "https://www.carousell.sg/"


class StubClient:
    """A browser that answers the login probe from a script and records what it was asked to do."""

    def __init__(self, *, login="logged_out", fail=None):
        self.login = login
        self.fail = fail
        self.navigations: list = []
        self.frontmost: list = []

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
        if self.fail == "navigate":
            from sellee.browser.client import BrowserToolError

            raise BrowserToolError("navigation refused")
        self.navigations.append(url)

    def ensure_frontmost(self, url):
        self.frontmost.append(url)

    def evaluate(self, function, **kwargs):
        if function == carousell_market.LOGIN_JS:
            return {"state": self.login}
        raise AssertionError(f"the lane evaluated an artifact this stub does not know: {function}")


def _deps(store, bus, client, **overrides):
    def factory():
        return client

    return connect.ConnectDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=overrides.pop("browser_factory", factory),
        **overrides,
    )


@pytest.fixture(autouse=True)
def _region(store):
    """Every marketplace URL is regional, so a seller with no region has no site to open."""
    store.set_seller_config_section("basics", {"region": "SG"})


def _command(text):
    return {"kind": "command", "text": text, "payload": {}}


def _tap(choice, ref="carousell"):
    return {"kind": "action", "text": choice, "payload": {"ref": ref, "choice": choice}}


def _texts(store):
    return [notice["text"] for notice in store.claim_queued_notices(10)]


def _notices(store):
    return list(store.claim_queued_notices(10))


# --- the tap ------------------------------------------------------------------------------------


def test_the_signin_button_writes_a_request_and_touches_no_browser(store, bus) -> None:
    """The whole reason for the row: this runs on the receive loop, and a cold Chrome takes
    seconds to tens of seconds to come up."""
    text, controls = fastpaths.handle_fast_path(store, bus, _tap(fastpaths.CB_CONNECT_MARKET))

    assert store.pending_market_connects() == [
        {
            "market": "carousell",
            "mode": CONNECT_MODE_OPEN,
            "requested_ts": pytest.approx(store.pending_market_connects()[0]["requested_ts"]),
        }
    ]
    assert "Carousell" in text
    assert controls is None


def test_the_check_again_button_asks_for_a_probe_not_another_open(store, bus) -> None:
    """The seller has already signed in on that tab; re-opening would navigate away from it."""
    fastpaths.handle_fast_path(store, bus, _tap(fastpaths.CB_CONNECT_PROBE))

    assert store.pending_market_connects()[0]["mode"] == CONNECT_MODE_PROBE


def test_a_double_tap_is_one_request(store, bus) -> None:
    """Opening a marketplace navigates the daemon's one shared tab, so two requests for the same
    market must never both run."""
    fastpaths.handle_fast_path(store, bus, _tap(fastpaths.CB_CONNECT_MARKET))
    fastpaths.handle_fast_path(store, bus, _tap(fastpaths.CB_CONNECT_MARKET))

    assert len(store.pending_market_connects()) == 1


def test_the_newest_tap_wins_its_mode(store, bus) -> None:
    """A seller who taps Check again while an open is still pending is telling us they are already
    looking at the page — honor what they are looking at now."""
    fastpaths.handle_fast_path(store, bus, _tap(fastpaths.CB_CONNECT_MARKET))
    fastpaths.handle_fast_path(store, bus, _tap(fastpaths.CB_CONNECT_PROBE))

    assert store.pending_market_connects()[0]["mode"] == CONNECT_MODE_PROBE


def test_a_stale_button_for_a_withdrawn_market_says_so(store, bus) -> None:
    """Buttons live forever in a chat history; the registry does not."""
    text, controls = fastpaths.handle_fast_path(
        store, bus, _tap(fastpaths.CB_CONNECT_MARKET, ref="myspace")
    )

    assert store.pending_market_connects() == []
    assert "myspace" in text
    assert controls is None


def test_the_sign_in_button_says_where_the_signing_in_happens() -> None:
    """It is tapped on a phone and acted on at a computer. A bare "Sign in" reads like something
    the phone is about to do, which is the one thing it is not."""
    assert "desktop" in fastpaths.SIGN_IN_LABEL


def test_every_surface_attaches_the_same_door(store, bus) -> None:
    """Three places offer the sign-in button — the read lane's notice and the connect lane's two
    retries. A seller who sees one door worded three ways cannot tell it is one door."""
    from sellee.browser import inbox

    seen = set()

    inbox._notify_once(  # noqa: SLF001 — the notice builder is the surface under test
        inbox.InboxDeps(store=store, bus=bus, config=Config(), browser_factory=None),
        "logged_out:carousell",
        "x",
        controls=fastpaths.signin_controls("carousell"),
    )
    store.request_market_connect("carousell", CONNECT_MODE_OPEN)
    connect.connect_lane(_deps(store, bus, StubClient(fail="navigate")))

    for notice in _notices(store):
        if notice["controls"]:
            seen.update(tuple(c) for c in notice["controls"])
    assert seen == {(fastpaths.SIGN_IN_LABEL, f"carousell:{fastpaths.CB_CONNECT_MARKET}")}


def test_connect_and_the_buttons_are_answered_without_a_pass(store) -> None:
    assert fastpaths.is_fast_path(_command("/connect"))
    assert fastpaths.is_fast_path(_tap(fastpaths.CB_CONNECT_MARKET))
    assert fastpaths.is_fast_path(_tap(fastpaths.CB_CONNECT_PROBE))


# --- /connect resolving the market ---------------------------------------------------------------


def test_connect_with_one_market_switched_on_just_opens_it(store, bus) -> None:
    seed_setting(store, "connected_markets", ["carousell"])

    text, controls = fastpaths.handle_fast_path(store, bus, _command("/connect"))

    assert store.pending_market_connects()[0]["market"] == "carousell"
    assert controls is None
    assert "Carousell" in text


def test_connect_with_several_switched_on_asks_which(store, bus, monkeypatch) -> None:
    """The command carries no argument — providers normalize a command to its first word — so an
    ambiguous answer has to be a question, and buttons make it a tap rather than a spelling.

    Carousell is the only market with a browser adapter today, so the second one is stubbed in:
    this is the branch that has to already work when the next adapter lands.
    """
    monkeypatch.setattr(fastpaths.market_adapters, "supported_markets", lambda: ["carousell", "fb"])
    seed_setting(store, "connected_markets", ["carousell", "fb"])

    text, controls = fastpaths.handle_fast_path(store, bus, _command("/connect"))

    assert store.pending_market_connects() == []
    assert text == fastpaths.CONNECT_PICK
    assert controls == [
        ("Carousell", f"carousell:{fastpaths.CB_CONNECT_MARKET}"),
        ("Facebook Marketplace", f"fb:{fastpaths.CB_CONNECT_MARKET}"),
    ]


def test_connect_with_nothing_switched_on_says_so(store, bus) -> None:
    seed_setting(store, "connected_markets", [])

    text, controls = fastpaths.handle_fast_path(store, bus, _command("/connect"))

    assert store.pending_market_connects() == []
    assert text == fastpaths.CONNECT_NONE
    assert controls is None


def test_connect_never_offers_carousell_ai(store, bus) -> None:
    """carousell.ai is reached with an API key, so there is no window to open and nothing for the
    seller to type into."""
    seed_setting(store, "connected_markets", ["carousell-ai", "carousell"])

    _text, controls = fastpaths.handle_fast_path(store, bus, _command("/connect"))

    assert controls is None  # resolved to the one browser market, not a two-way picker
    assert store.pending_market_connects()[0]["market"] == "carousell"


# --- the lane -------------------------------------------------------------------------------------


def test_the_lane_opens_the_market_and_asks_the_seller_to_sign_in(store, bus) -> None:
    store.request_market_connect("carousell", CONNECT_MODE_OPEN)
    client = StubClient(login="logged_out")

    connect.connect_lane(_deps(store, bus, client))

    assert client.navigations == [_HOME]
    assert client.frontmost == [_HOME]  # asked for a window, so the tab comes forward
    notice = _notices(store)[0]
    assert "sign-in page is open" in notice["text"]
    assert notice["controls"] == [
        [fastpaths.CHECK_AGAIN_LABEL, f"carousell:{fastpaths.CB_CONNECT_PROBE}"]
    ]
    assert store.pending_market_connects() == []


def test_a_probe_does_not_elbow_the_seller_mid_sign_in(store, bus) -> None:
    """Check again must not reorder tabs — they are typing a password into one of them."""
    store.request_market_connect("carousell", CONNECT_MODE_PROBE)
    client = StubClient(login="logged_out")

    connect.connect_lane(_deps(store, bus, client))

    assert client.navigations == [_HOME]
    assert client.frontmost == []
    assert "still see a login screen" in _texts(store)[0]


def test_a_signed_in_market_is_confirmed_and_the_request_cleared(store, bus) -> None:
    store.request_market_connect("carousell", CONNECT_MODE_PROBE)

    connect.connect_lane(_deps(store, bus, StubClient(login="logged_in")))

    notice = _notices(store)[0]
    assert "Signed in to Carousell" in notice["text"]
    assert notice["controls"] is None  # nothing left to tap
    assert store.pending_market_connects() == []


def test_an_unknown_probe_is_not_reported_as_signed_in(store, bus) -> None:
    """`unknown` is "no answer". Claiming a sign-in we did not see would send the seller away
    believing their market is being read."""
    store.request_market_connect("carousell", CONNECT_MODE_PROBE)

    connect.connect_lane(_deps(store, bus, StubClient(login="unknown")))

    assert "Signed in" not in _texts(store)[0]


def test_the_login_state_reaches_the_event_log(store, bus) -> None:
    store.request_market_connect("carousell", CONNECT_MODE_OPEN)

    connect.connect_lane(_deps(store, bus, StubClient(login="logged_in")))

    events = bus.store.read(kinds=["browser.login"])
    assert [(e.payload["market"], e.payload["state"]) for e in events] == [
        ("carousell", "logged_in")
    ]


def test_a_pass_driving_the_browser_leaves_the_request_pending(store, bus) -> None:
    """A publish mid-drive owns the tab. Navigating it now would pull the page out from under a
    half-filled composer — the seller asked to sign in, not to lose a listing."""
    store.enqueue_pass("publish", {"market": "carousell"})
    store.request_market_connect("carousell", CONNECT_MODE_OPEN)
    client = StubClient()

    connect.connect_lane(_deps(store, bus, client))

    assert client.navigations == []
    assert store.pending_market_connects() != []  # retried next tick, not dropped
    assert store.count_queued_notices() == 0  # and not answered wrongly meanwhile


def test_a_request_the_browser_never_got_to_is_dropped_with_a_notice(store, bus) -> None:
    """Better one honest "I couldn't" than a request that retries in silence forever."""
    store.enqueue_pass("publish", {"market": "carousell"})
    store.request_market_connect("carousell", CONNECT_MODE_OPEN)
    # The row is stamped by the store's own clock, so the lane's has to be read off it.
    requested = store.pending_market_connects()[0]["requested_ts"]
    late = {"t": requested}

    deps = _deps(store, bus, StubClient(), now=lambda: late["t"])
    connect.connect_lane(deps)
    assert store.count_queued_notices() == 0

    late["t"] = requested + connect.STALE_REQUEST_SEC + 1
    connect.connect_lane(deps)
    connect.connect_lane(deps)  # the row is gone, so this adds nothing

    notice = _notices(store)[0]
    assert "stayed busy" in notice["text"]
    assert notice["controls"] == [
        [fastpaths.SIGN_IN_LABEL, f"carousell:{fastpaths.CB_CONNECT_MARKET}"]
    ]
    assert store.pending_market_connects() == []


def test_a_browser_that_cannot_be_driven_falls_back_to_the_cli(store, bus) -> None:
    """The one case where the shell is still the way out — so this is where the CLI is named."""
    store.request_market_connect("carousell", CONNECT_MODE_OPEN)

    connect.connect_lane(_deps(store, bus, StubClient(fail="navigate")))

    notice = _notices(store)[0]
    assert "`sellee connect carousell`" in notice["text"]
    assert notice["controls"] == [
        [fastpaths.SIGN_IN_LABEL, f"carousell:{fastpaths.CB_CONNECT_MARKET}"]
    ]
    assert store.pending_market_connects() == []


def test_no_browser_at_all_is_reported_not_raised(store, bus) -> None:
    from sellee.browser.client import BrowserUnavailable

    def factory():
        raise BrowserUnavailable("npx not found")

    store.request_market_connect("carousell", CONNECT_MODE_OPEN)
    connect.connect_lane(_deps(store, bus, None, browser_factory=factory))

    assert "npx not found" in _texts(store)[0]
    assert store.pending_market_connects() == []


def test_an_unset_region_is_reported_rather_than_navigated(store, bus) -> None:
    store.set_seller_config_section("basics", {})
    store.request_market_connect("carousell", CONNECT_MODE_OPEN)
    client = StubClient()

    connect.connect_lane(_deps(store, bus, client))

    assert client.navigations == []
    assert "no site for" in _texts(store)[0]


def test_a_market_with_no_adapter_is_cleared_not_retried_forever(store, bus) -> None:
    store.request_market_connect("carousell", CONNECT_MODE_OPEN)
    with store._db.transaction() as conn:  # noqa: SLF001 — arranging a withdrawn registry entry
        conn.execute("UPDATE market_connect_requests SET market = 'myspace'")

    connect.connect_lane(_deps(store, bus, StubClient()))

    assert store.pending_market_connects() == []
    assert "myspace" in _texts(store)[0]


def test_each_market_is_served_on_its_own(store, bus) -> None:
    store.request_market_connect("carousell", CONNECT_MODE_PROBE)
    store.request_market_connect("fb", CONNECT_MODE_PROBE)

    connect.connect_lane(_deps(store, bus, StubClient(login="logged_in")))

    assert store.pending_market_connects() == []
    assert len(_notices(store)) == 2


def test_the_lane_is_a_no_op_with_nothing_pending(store, bus) -> None:
    client = StubClient()

    connect.connect_lane(_deps(store, bus, client))

    assert client.navigations == []
    assert store.count_queued_notices() == 0
