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

    def navigate_visible(self, url):
        """A read brings the tab forward first; for a stub that is just a navigation."""
        self.navigate(url)

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


# --- reaching the seller's listings ---------------------------------------------------------------


class SurveyStub:
    """A browser answering Facebook's survey artifacts, recording where the lane sent it."""

    def __init__(self, *, entry_url="/marketplace/profile/1513211510/", listings=None):
        self.entry_url = entry_url
        self.listings = listings if listings is not None else {"listings": [], "active_count": 0}
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
        self.navigations.append(url)

    def evaluate(self, function, **kwargs):
        if function == fb_market.LOGIN_JS:
            return {"state": "logged_in"}
        if function == fb_market.MY_LISTINGS_ENTRY_JS:
            return {"url": self.entry_url, "width": 1600, "visible": True}
        if function == fb_market.MY_LISTINGS_JS:
            return self.listings
        raise AssertionError(f"the lane evaluated an artifact this stub does not know: {function}")


def _survey_deps(store, bus, client):
    from sellee.browser import survey

    return survey.SurveyDeps(store=store, bus=bus, config=Config(), browser_factory=lambda: client)


def test_the_survey_follows_the_link_to_the_page_that_has_the_ids(store, bus) -> None:
    """`/marketplace/you/selling` shows the listings and gives them no id, so a survey that read it
    could record nothing. The ids are on the seller's own profile, one link away."""
    from sellee.browser import survey

    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    client = SurveyStub()

    survey.discover_phase(_survey_deps(store, bus, client))

    assert client.navigations == [
        "https://www.facebook.com/marketplace/you/selling",
        "https://www.facebook.com/marketplace/profile/1513211510/",
    ]


def test_a_missing_profile_link_is_an_unserved_survey_not_an_empty_one(store, bus) -> None:
    """The ask-once guard closes on an empty list, so a page we never reached must never answer as
    one — that would tell the seller we looked and found nothing listed."""
    from sellee.browser import survey

    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    client = SurveyStub(entry_url=None)

    survey.discover_phase(_survey_deps(store, bus, client))

    assert store.get_market_survey("fb")["state"] == "due"
    reasons = [e.payload["reason"] for e in _kinds(bus, "survey.unserved")]
    assert reasons == ["could not reach the seller's listings page"]


def test_a_market_whose_listings_page_is_an_address_takes_no_hop(
    store, bus, carousell_only
) -> None:
    """Carousell's manage-listings page is the page. The hop must stay inert for it."""
    from sellee.browser import survey
    from sellee.browser.markets import carousell as carousell_market

    class CarousellSurveyStub(SurveyStub):
        def evaluate(self, function, **kwargs):
            if function == carousell_market.LOGIN_JS:
                return {"state": "logged_in"}
            if function == carousell_market.MY_LISTINGS_JS:
                return {"listings": [], "active_count": 0}
            raise AssertionError(f"unexpected artifact: {function}")

    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("carousell")
    client = CarousellSurveyStub()

    survey.discover_phase(_survey_deps(store, bus, client))

    assert client.navigations == ["https://www.carousell.sg/manage-listings/"]


# --- one thing on two marketplaces ---------------------------------------------------------------


def _managed_fb_listing(store, listing_id="222", title="White Study Desk"):
    """An accepted Facebook listing waiting to be adopted, as a yes on the survey leaves it."""
    store.record_survey_result(
        "fb",
        [
            {
                "listing_id": listing_id,
                "url": f"https://www.facebook.com/marketplace/item/{listing_id}/",
                "title": title,
                "price": 65.0,
                "price_text": "SGD65",
            }
        ],
    )
    store.decide_discovered_listings("fb", decision="manage", manage="relist")


def _twin(store, *, rail=True, title="White Study Desk"):
    """The same thing, already managed from Carousell."""
    item = store.create_item(title=title, list_price=65.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell", "https://www.carousell.sg/p/desk-1/")
    if rail:
        store.record_listing_url(item["id"], "carousell-ai", "https://carousell.ai/l/desk")
    return item


def _merge_deps(store, bus):
    """Adoption deps whose browser would raise: a merge is settled from rows alone, and touching
    the marketplace to decide it would be a bug worth failing on."""
    from sellee.browser import survey

    def no_browser():
        raise AssertionError("a merge must be decided without reading the marketplace")

    return survey.SurveyDeps(store=store, bus=bus, config=Config(), browser_factory=no_browser)


def test_the_same_thing_on_two_marketplaces_becomes_one_item(store, bus) -> None:
    """A seller who cross-lists by hand has one desk, not two. The Facebook copy of a desk already
    managed on Carousell links to it rather than making a second item — two items would mean two
    carousell.ai listings, two floors, and buyers from each market on a different row."""
    from sellee.browser import adopt

    item = _twin(store)
    _managed_fb_listing(store)

    adopt.adopt_phase(_merge_deps(store, bus))

    assert [i["id"] for i in store.list_items()] == [item["id"]], "a second item was created"
    merged = store.get_item(item["id"])
    assert merged["listing_urls"]["fb"] == "https://www.facebook.com/marketplace/item/222/"
    assert merged["listing_urls"]["carousell"] == "https://www.carousell.sg/p/desk-1/"


def test_a_merged_listing_is_not_published_to_the_rail_a_second_time(store, bus) -> None:
    """The point of merging: the twin already has its carousell.ai listing, so this owes none."""
    from sellee.browser import adopt

    _twin(store)
    _managed_fb_listing(store)

    adopt.adopt_phase(_merge_deps(store, bus))

    assert store.listings_owed_rail_publish() == []


def test_merging_does_not_swallow_a_rail_publish_that_is_still_owed(store, bus) -> None:
    """An item the seller had answered-only, matched from a second marketplace they asked to
    relist, is owed its carousell.ai listing — once."""
    from sellee.browser import adopt

    _twin(store, rail=False)
    _managed_fb_listing(store)

    adopt.adopt_phase(_merge_deps(store, bus))

    assert [r["listing_id"] for r in store.listings_owed_rail_publish()] == ["222"]


def test_two_items_sharing_a_title_are_never_merged_into_either(store, bus) -> None:
    """Which one this listing is cannot be settled from a title alone, and merging into whichever
    came first would put a buyer on the wrong floor. Left for the seller."""
    from sellee.browser import adopt

    for suffix in ("a", "b"):
        it = store.create_item(title="White Study Desk", list_price=65.0, currency="SGD")
        store.record_listing_url(
            it["id"], "carousell", f"https://www.carousell.sg/p/desk-{suffix}/"
        )
    _managed_fb_listing(store)

    adopt.adopt_phase(_merge_deps(store, bus))

    assert len(store.list_items()) == 2, "a third item was created"
    assert store.list_discovered_listings("fb")[0]["status"] == "failed"


def test_the_survey_does_not_ask_again_about_a_thing_already_managed(store, bus) -> None:
    """Asking "want me to manage this desk?" about a desk already being managed reads as though
    the first answer was lost."""
    from sellee.browser import survey

    _twin(store)
    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    client = SurveyStub(
        listings={
            "listings": [
                {
                    "listing_id": "222",
                    "url": "https://www.facebook.com/marketplace/item/222/",
                    "title": "White Study Desk",
                    "price": 65.0,
                    "price_text": "SGD65",
                }
            ],
            "active_count": 1,
        }
    )

    survey.discover_phase(_survey_deps(store, bus, client))

    assert store.list_discovered_listings("fb") == []


def test_a_near_miss_title_is_two_different_products() -> None:
    """One real inventory holds both of these. Anything looser than whole-title equality fuses
    them, and then a buyer for the hooks negotiates over the clips."""
    from sellee.browser import reconcile

    items = [
        {"id": "a", "title": "Monster Open-Ear Clip Wireless Earbuds", "listing_urls": {}},
        {"id": "b", "title": "Monster Open Ear Hook Wireless Earbuds", "listing_urls": {}},
    ]

    assert reconcile.items_for_same_listing(
        "Monster Open-Ear Clip Wireless Earbuds", items, "fb"
    ) == ["a"]
    assert reconcile.items_for_same_listing("Monster Earbuds", items, "fb") == []


def test_an_item_already_listed_on_this_market_is_not_a_twin() -> None:
    """Two same-titled listings on ONE marketplace are two of the thing, not one seen twice."""
    from sellee.browser import reconcile

    items = [{"id": "a", "title": "IKEA Elloven", "listing_urls": {"fb": "https://fb/1"}}]

    assert reconcile.items_for_same_listing("IKEA Elloven", items, "fb") == []
    assert reconcile.items_for_same_listing("IKEA Elloven", items, "carousell") == ["a"]


# --- recognising the seller's own cross-listing --------------------------------------------------


def test_recognising_a_twin_records_the_url_on_the_item(store, bus) -> None:
    """Recognising it and writing nothing down was a hole with real consequences: the listing left
    the ask, but the item still carried no Facebook URL, so the fan-out believed it was absent from
    Facebook and would have posted a second copy of the very listing just recognised."""
    from sellee.browser import survey

    item = _twin(store)
    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    client = SurveyStub(
        listings={
            "listings": [
                {
                    "listing_id": "222",
                    "url": "https://www.facebook.com/marketplace/item/222/",
                    "title": "White Study Desk",
                    "price": 65.0,
                    "price_text": "SGD65",
                }
            ],
            "active_count": 1,
        }
    )

    survey.discover_phase(_survey_deps(store, bus, client))

    assert store.get_item(item["id"])["listing_urls"]["fb"] == (
        "https://www.facebook.com/marketplace/item/222/"
    )
    assert store.list_discovered_listings("fb") == []  # still not asked about


def test_a_recognised_twin_is_not_published_again(store, bus) -> None:
    """The end the linking exists for."""
    from sellee import crosslist
    from sellee.browser import survey

    item = _twin(store)
    store.record_listing_url(item["id"], "carousell-ai", "https://carousell.ai/l/desk")
    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    client = SurveyStub(
        listings={
            "listings": [
                {
                    "listing_id": "222",
                    "url": "https://www.facebook.com/marketplace/item/222/",
                    "title": "White Study Desk",
                    "price": 65.0,
                    "price_text": "SGD65",
                }
            ],
            "active_count": 1,
        }
    )
    survey.discover_phase(_survey_deps(store, bus, client))

    deps = crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=lambda: None,
    )
    assert [m for _i, m in crosslist.pending_pairs(deps) if m == "fb"] == []


def test_two_items_with_one_title_are_linked_to_neither(store, bus) -> None:
    """Same discipline as adoption: with two items sharing a title there is no telling from the
    page which one this listing is, and guessing puts a buyer on the wrong item's floor."""
    from sellee.browser import survey

    for suffix in ("a", "b"):
        it = store.create_item(title="White Study Desk", list_price=65.0, currency="SGD")
        store.record_listing_url(
            it["id"], "carousell", f"https://www.carousell.sg/p/desk-{suffix}/"
        )
    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    client = SurveyStub(
        listings={
            "listings": [
                {
                    "listing_id": "222",
                    "url": "https://www.facebook.com/marketplace/item/222/",
                    "title": "White Study Desk",
                    "price": 65.0,
                    "price_text": "SGD65",
                }
            ],
            "active_count": 1,
        }
    )

    survey.discover_phase(_survey_deps(store, bus, client))

    assert all("fb" not in i["listing_urls"] for i in store.list_items())


# --- what currency a listing's price is in --------------------------------------------------------


@pytest.mark.parametrize(
    "detail,basics,expected",
    [
        # A code on the page wins outright.
        ({"currency": "SGD", "price_text": "SGD65"}, {"currency": "SGD"}, "SGD"),
        ({"currency": "sgd", "price_text": ""}, {}, "SGD"),
        # A symbol the marketplace prints instead, resolved unambiguously.
        ({"currency": "", "price_text": "S$65"}, {"currency": "USD"}, "SGD"),
        ({"currency": "", "price_text": "RM 1,200"}, {}, "MYR"),
        ({"currency": "", "price_text": "HK$40"}, {}, "HKD"),
        ({"currency": "", "price_text": "Rp1.500.000"}, {}, "IDR"),
        # A bare "$" is USD, SGD, AUD and more — so the seller's own currency answers it, which is
        # the case that matters most: a US seller has no other marketplace at all.
        ({"currency": "", "price_text": "$65"}, {"currency": "USD"}, "USD"),
        ({"currency": "", "price_text": "$65"}, {"currency": "SGD"}, "SGD"),
        # Nothing anywhere is still nothing — the caller refuses rather than inventing one.
        ({"currency": "", "price_text": "65"}, {}, ""),
    ],
)
def test_the_currency_is_resolved_from_the_page_then_the_seller(
    store, detail, basics, expected
) -> None:
    """Scraping /[A-Z]{3}/ out of the price text matched "SGD65" and nothing else, so adoption
    failed terminally with "no usable price" for every seller whose marketplace prints a symbol."""
    from sellee.browser import adopt

    if basics:
        store.set_seller_config_section("basics", {"region": "SG", **basics})

    assert adopt._currency_for(store, detail) == expected


def test_a_us_seller_can_adopt_a_dollar_priced_listing(store, bus) -> None:
    """The regression in one line: Facebook shows a US seller "$65", and before this they could
    adopt nothing at all."""
    from sellee.browser import adopt

    store.set_seller_config_section("basics", {"region": "US", "currency": "USD"})

    assert adopt._currency_for(store, {"currency": "", "price_text": "$65"}) == "USD"


# --- what the system believes exists on a marketplace ---------------------------------------------


def test_a_near_miss_is_withheld_even_after_the_seller_declines(store, bus) -> None:
    """The decline path was the hole. The seller is told "I'll leave your Facebook listings alone",
    and on the next tick the fan-out posted a second copy of the very book they meant."""
    from sellee import crosslist
    from sellee.browser import survey

    item = store.create_item(
        title="If Anyone Builds It, Everyone Dies by Yudkowsky & Soares",
        list_price=20.0,
        currency="SGD",
    )
    store.record_listing_url(item["id"], "carousell-ai", "https://carousell.ai/l/book")
    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    survey.discover_phase(
        _survey_deps(
            store,
            bus,
            SurveyStub(
                listings={
                    "listings": [
                        {
                            "listing_id": "77",
                            "url": "https://www.facebook.com/marketplace/item/77/",
                            "title": "If Anyone Builds It, Everyone Dies (Yudkowsky & Soares)",
                            "price": 20.0,
                            "price_text": "SGD20",
                        }
                    ],
                    "active_count": 1,
                },
            ),
        )
    )
    store.decide_discovered_listings("fb", decision="decline")

    deps = crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=lambda: None,
    )
    assert [m for _i, m in crosslist.pending_pairs(deps) if m == "fb"] == []


def test_a_near_miss_is_refused_rather_than_adopted_as_a_second_item(store, bus) -> None:
    """The accept path: adopting made a second item, then a second rail listing, and then fanned
    the first one out to the marketplace this listing was already on."""
    from sellee.browser import adopt

    item = store.create_item(
        title="If Anyone Builds It, Everyone Dies by Yudkowsky & Soares",
        list_price=20.0,
        currency="SGD",
    )
    store.record_listing_url(item["id"], "carousell", "https://www.carousell.sg/p/book-1/")
    store.record_survey_result(
        "fb",
        [
            {
                "listing_id": "77",
                "url": "https://www.facebook.com/marketplace/item/77/",
                "title": "If Anyone Builds It, Everyone Dies (Yudkowsky & Soares)",
                "price": 20.0,
                "price_text": "SGD20",
            }
        ],
    )
    store.decide_discovered_listings("fb", decision="manage", manage="relist")

    adopt.adopt_phase(_merge_deps(store, bus))

    assert len(store.list_items()) == 1, "a second item was created for one book"
    assert store.list_discovered_listings("fb")[0]["status"] == "failed"


def test_a_sold_item_never_captures_a_live_listing(store, bus) -> None:
    """A seller sells one of two identical chairs. Matching the sold one to the live listing puts
    its URL on a closed sale, and from then on every buyer on it is told "it's sold"."""
    from sellee.browser import reconcile

    sold_item = store.create_item(title="Herman Miller Aeron", list_price=500.0, currency="SGD")
    live_item = store.create_item(title="Herman Miller Aeron", list_price=500.0, currency="SGD")
    items = store.list_items()

    without = reconcile.items_for_same_listing("Herman Miller Aeron", items, "fb")
    with_sold = reconcile.items_for_same_listing(
        "Herman Miller Aeron", items, "fb", {sold_item["id"]}
    )

    assert set(without) == {sold_item["id"], live_item["id"]}
    assert with_sold == [live_item["id"]], "the sold item is still a candidate"


def test_an_unreadable_row_stops_the_survey_closing_as_complete(store, bus) -> None:
    """Both readers count a dropped row as read when they compute `truncated`, so 14 of 17 with 3
    unparseable arrives claiming to be complete — and the 3 leave no trace for the fan-out."""
    from sellee.browser import survey

    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    client = SurveyStub(
        listings={
            "listings": [
                {
                    "listing_id": "1",
                    "url": "https://www.facebook.com/marketplace/item/1/",
                    "title": "A thing",
                    "price": 10.0,
                    "price_text": "SGD10",
                }
            ],
            "active_count": 4,
            "unreadable": 3,
            "truncated": False,
        }
    )

    survey.discover_phase(_survey_deps(store, bus, client))

    assert store.get_market_survey("fb")["state"] == "due", "closed on a page it could not read"
    assert store.list_discovered_listings("fb") == []


def test_an_ambiguous_twin_is_asked_about_rather_than_dropped(store, bus) -> None:
    """Dropped silently, it existed on the marketplace and in none of our tables — which the
    fan-out reads as an absence and duplicates."""
    from sellee.browser import survey

    for suffix in ("a", "b"):
        it = store.create_item(title="White Study Desk", list_price=65.0, currency="SGD")
        store.record_listing_url(
            it["id"], "carousell", f"https://www.carousell.sg/p/desk-{suffix}/"
        )
    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})
    store.request_market_survey("fb")
    client = SurveyStub(
        listings={
            "listings": [
                {
                    "listing_id": "222",
                    "url": "https://www.facebook.com/marketplace/item/222/",
                    "title": "White Study Desk",
                    "price": 65.0,
                    "price_text": "SGD65",
                }
            ],
            "active_count": 1,
        }
    )

    survey.discover_phase(_survey_deps(store, bus, client))

    assert [r["listing_id"] for r in store.list_discovered_listings("fb")] == ["222"]
