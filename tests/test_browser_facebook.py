"""Facebook's inbox read: opening a folder that has no URL, and finding the listing behind a
conversation that only names it by title.

Everything here is about the two things Facebook does that Carousell does not, because those are
the two places the generic lane grew a seam. The artifacts themselves are stubbed — a selector is
only ever proved against the live page — so what these tests hold is the wiring: that the folder is
opened before the list is read, that a folder which did not open is reported as blindness rather
than as an empty inbox, and that a conversation is never attached to an item on anything weaker than
the listing id.
"""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

from sellee.browser import blindness, inbox
from sellee.browser.client import BrowserToolError
from sellee.browser.markets import facebook as fb_market
from sellee.config import Config

_THREAD = "https://www.facebook.com/messages/t/99/"
_LISTING = "https://www.facebook.com/marketplace/item/2360069924525441/"
_PRODUCT_ID = "2360069924525441"


class StubClient:
    """A browser answering Facebook's artifacts from a script, and recording what it was clicked."""

    def __init__(
        self,
        *,
        login="logged_in",
        conversations=(),
        list_error=None,
        folder_marked=True,
        product_id=_PRODUCT_ID,
        tails=None,
        click_fails=False,
    ):
        self.login = login
        self.conversations = conversations
        self.list_error = list_error
        self.folder_marked = folder_marked
        self.product_id = product_id
        self.tails = tails or {}
        self.click_fails = click_fails
        self.navigations: list = []
        self.clicks: list = []
        self.calls: list = []
        self.url = ""

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
        self.navigations.append(url)
        self.calls.append(("navigate", url))
        self.url = url

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments.get("target")))
        if name == "browser_click":
            if self.click_fails:
                raise BrowserToolError("no element matches")
            self.clicks.append(arguments)
        return ""

    def evaluate(self, function, **kwargs):
        # Dispatched on the adapter's own artifacts, so moving one shows up here as a missing case
        # rather than as a substring match landing on the wrong branch.
        if function == fb_market.LOGIN_JS:
            return {"state": self.login}
        if function == fb_market.INBOX_FOLDER_JS:
            return {"marked": self.folder_marked, "candidates": 1, "width": 1600, "visible": True}
        if function == fb_market.CONVERSATIONS_LIST_JS:
            if self.list_error is not None:
                return {"error": self.list_error, "rows": 0, "width": 756, "visible": True}
            return {"conversations": list(self.conversations)}
        if function == fb_market.PRODUCT_ID_JS:
            return {"product_id": self.product_id, "visible": True}
        if function == fb_market.CONVERSATION_TAIL_JS:
            native = self.url.rstrip("/").rsplit("/", 1)[-1]
            return list(self.tails.get(native, []))
        raise AssertionError(f"the lane evaluated an artifact this stub does not know: {function}")


@pytest.fixture(autouse=True)
def _fb_only(store):
    """Just Facebook connected: a lane tick drives every connected market, and this file scripts
    Facebook's artifacts and nothing else."""
    seed_setting(store, "connected_markets", ["fb"])
    return store


@pytest.fixture
def seeded(store):
    """An item listed on Facebook, so a conversation about it is recognisable by listing id."""
    store.set_seller_config_section("basics", {"region": "SG"})
    item = store.create_item(title="White study desk", list_price=65.0, currency="SGD")
    store.record_listing_url(item["id"], "fb", _LISTING)
    return store.get_item(item["id"])


def _deps(store, bus, client):
    clock = {"t": 1000.0}

    def now():
        clock["t"] += 1.0
        return clock["t"]

    return inbox.InboxDeps(
        store=store, bus=bus, config=Config(), browser_factory=lambda: client, now=now
    )


def _conv(**overrides):
    """One conversation as the Marketplace folder reports it — note `product_id` is absent, which
    is the whole point: the folder names the listing by title only."""
    row = {
        "thread_id": "99",
        "handle": "Gerry",
        "product_id": None,
        "title": "White study desk",
        "unread": 0,
        "last_message": "Gerry: is this still available?",
    }
    row.update(overrides)
    return row


def _kinds(bus, kind):
    return bus.store.read(kinds=[kind])


# --- opening a folder that has no URL ------------------------------------------------------------


def test_the_folder_is_opened_by_a_real_click_before_the_list_is_read(store, bus, seeded) -> None:
    """The control ignores a click dispatched from the page, so the adapter marks it and the lane
    clicks the mark for real — and it has to happen before the list read, or the list read answers
    with the seller's personal inbox."""
    client = StubClient(conversations=[_conv()])

    inbox.inbox_lane(_deps(store, bus, client))

    assert client.clicks, "the lane never clicked the folder open"
    assert client.clicks[0]["target"] == fb_market.INBOX_FOLDER_TARGET
    clicked = [i for i, (name, _) in enumerate(client.calls) if name == "browser_click"]
    assert clicked, "no click was recorded at all"


def test_a_market_whose_inbox_is_a_page_is_never_clicked(store, bus, carousell_only) -> None:
    """Carousell's inbox is an address, so there is nothing to open. The seam must stay inert for
    it rather than clicking at whatever happens to be on the page."""
    from sellee.browser.markets import carousell as carousell_market

    class CarousellStub(StubClient):
        def evaluate(self, function, **kwargs):
            if function == carousell_market.LOGIN_JS:
                return {"state": "logged_in"}
            if function == carousell_market.CONVERSATIONS_LIST_JS:
                return {"conversations": []}
            raise AssertionError(f"unexpected artifact: {function}")

    client = CarousellStub()

    inbox.inbox_lane(_deps(store, bus, client))

    assert client.clicks == []


def test_a_folder_that_will_not_open_still_lets_the_read_report_for_itself(
    store, bus, seeded
) -> None:
    """A click that fails must not raise out of the lane. The list artifact proves the folder is
    open for itself, so the honest failure comes from there, with its measurements attached."""
    client = StubClient(click_fails=True, list_error="the Marketplace folder is not open")

    inbox.inbox_lane(_deps(store, bus, client))

    blind = _kinds(bus, "browser.blind")
    assert blind, "a folder that never opened must be reported, not passed over in silence"


def test_a_folder_that_did_not_open_is_blindness_and_never_an_empty_inbox(
    store, bus, seeded
) -> None:
    """The one answer that must never be guessed. An unopened folder leaves the personal inbox on
    screen; reporting that as "no marketplace conversations" would strand every buyer silently."""
    client = StubClient(list_error="the Marketplace folder is not open")

    inbox.inbox_lane(_deps(store, bus, client))

    blind = _kinds(bus, "browser.blind")
    assert blind, "a refusing reader must count as blind"
    payload = blind[-1].payload
    assert payload["market"] == "fb"
    # The reader measured a narrow window, so the cause names the window rather than the market —
    # the only one of the four the seller can actually act on.
    assert payload["cause"] == blindness.CAUSE_VIEWPORT
    assert _kinds(bus, "browser.read") == []


def test_an_unmarked_folder_control_does_not_stop_the_read(store, bus, seeded) -> None:
    """Nothing is clicked when the mark found no control, and the lane carries on to let the list
    artifact say what it can see."""
    client = StubClient(folder_marked=False, list_error="the Marketplace folder is not open")

    inbox.inbox_lane(_deps(store, bus, client))

    assert client.clicks == []
    assert _kinds(bus, "browser.blind")


# --- the listing behind a conversation -----------------------------------------------------------


def test_the_listing_id_is_read_from_the_opened_conversation_and_adopts(store, bus, seeded) -> None:
    """The folder names the listing by title; the id is on a banner inside the conversation. That
    read is what lets the thread be joined to the item at all."""
    client = StubClient(conversations=[_conv()], tails={"99": []})

    inbox.inbox_lane(_deps(store, bus, client))

    thread = store.get_thread("fb:99")
    assert thread is not None, "the conversation was never adopted"
    assert thread["item_id"] == seeded["id"]
    assert thread["counterpart_handle"] == "Gerry"
    assert any(_THREAD in url for url in client.navigations)


def test_a_conversation_whose_listing_cannot_be_read_is_never_guessed_onto_an_item(
    store, bus, seeded
) -> None:
    """A title is not something to match on. With no id the conversation is left alone and said to
    be about an unknown listing — the event that explains why a buyer is going unanswered."""
    client = StubClient(conversations=[_conv()], product_id=None)

    inbox.inbox_lane(_deps(store, bus, client))

    assert store.get_thread("fb:99") is None
    reasons = [e.payload["reason"] for e in _kinds(bus, "browser.unmatched")]
    assert reasons == ["unknown_listing"]


def test_a_listing_id_that_is_not_ours_does_not_adopt(store, bus, seeded) -> None:
    """The banner answered, and it named a listing the seller did not publish through us — most
    often one they made outside the agent."""
    client = StubClient(conversations=[_conv()], product_id="1111111111")

    inbox.inbox_lane(_deps(store, bus, client))

    assert store.get_thread("fb:99") is None
    assert [e.payload["reason"] for e in _kinds(bus, "browser.unmatched")] == ["unknown_listing"]


def test_the_conversation_is_not_reopened_for_a_thread_we_already_know(store, bus, seeded) -> None:
    """The id read costs a navigation, so it happens once — when the conversation is first seen —
    and never again on a thread that already carries its item."""
    store.create_thread(
        thread_id="fb:99",
        side="sell",
        market="fb",
        counterpart_handle="Gerry",
        item_id=seeded["id"],
    )
    client = StubClient(conversations=[_conv()], tails={"99": []})

    inbox.inbox_lane(_deps(store, bus, client))

    evaluated_ids = [c for c in client.calls if c[0] == "navigate" and c[1] == _THREAD]
    assert len(evaluated_ids) == 1, "the thread was navigated more than once for one read"


def test_a_row_the_folder_reports_with_an_id_is_taken_at_its_word(store, bus, seeded) -> None:
    """The seam is additive: a market whose list already carries the listing id must not pay for a
    second navigation, and Facebook's own rows would too if they ever started carrying one."""
    client = StubClient(conversations=[_conv(product_id=_PRODUCT_ID)], tails={"99": []})

    inbox.inbox_lane(_deps(store, bus, client))

    assert store.get_thread("fb:99") is not None
    assert client.navigations.count(_THREAD) == 1
