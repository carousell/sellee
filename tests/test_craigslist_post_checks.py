"""Craigslist posts after they go up: sellee picks each post's category, reads the post back on a
backoff, and a post Craigslist took down after a flag goes up again in the next category."""

from __future__ import annotations

import re
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting
from tests.test_craigslist_driver import FakeForm
from tests.test_craigslist_edit import FakePost
from tests.test_take_down_notice import _Chrome, _lane, _sold, _tap

from sellee import craigslist_posts, crosslist, revise
from sellee.browser.markets import craigslist
from sellee.config import Config
from sellee.rail.client import RailUnprovisioned

_RAIL_URL = "https://www.carousell.ai/listing/abc123"
_POST = "https://www.craigslist.org/view/d/samsung-buds3/9TXkjafxYMDS5VaPLGdRyk"
_TITLE = "Samsung Galaxy Buds3 Wireless Earbuds"
# The SF bay area category step as read live.
_LIVE_CATEGORIES = [
    "antiques", "appliances", "arts & crafts", "atvs, utvs, snowmobiles", "auto parts",
    "auto wheels & tires", "aviation", "baby & kid stuff", "barter", "bicycle parts", "bicycles",
    "boat parts", "boats", "books & magazines", "business/commercial", "cars & trucks ($5)",
    "cds / dvds / vhs", "cell phones", "clothing & accessories", "collectibles", "computer parts",
    "computers", "electronics", "farm & garden", "free stuff", "furniture",
    "garage & moving sales", "general for sale", "health and beauty", "heavy equipment",
    "household items", "jewelry", "materials", "motorcycle parts", "motorcycles/scooters ($5)",
    "musical instruments", "photo/video", "rvs ($5)", "sporting goods", "tickets", "tools",
    "toys & games", "trailers", "video gaming", "wanted",
]  # fmt: skip
_NEVER = {"barter", "free stuff", "wanted", "garage & moving sales", "business/commercial"}


# --- the categories sellee picks -----------------------------------------------------------------


def test_earbuds_go_in_electronics_first_and_the_catch_all_last() -> None:
    picked = craigslist.categories_for({"title": _TITLE, "description": "Barely used."})
    assert picked == ["electronics", "cell phones", craigslist.DEFAULT_CATEGORY]


def test_an_item_no_word_places_goes_in_the_catch_all() -> None:
    assert craigslist.categories_for({"title": "Thing"}) == [craigslist.DEFAULT_CATEGORY]


def test_every_keyed_category_is_one_the_live_step_offers() -> None:
    assert set(craigslist.CATEGORY_WORDS) <= set(_LIVE_CATEGORIES)


@settings(max_examples=200, deadline=None)
@given(
    title=st.lists(
        st.sampled_from([w for ws in craigslist.CATEGORY_WORDS.values() for w in ws.split("|")])
        | st.text(max_size=12),
        max_size=6,
    ).map(" ".join),
    description=st.text(max_size=80),
)
def test_the_picked_categories_are_few_free_and_end_with_the_catch_all(title, description) -> None:
    picked = craigslist.categories_for({"title": title, "description": description})

    assert 1 <= len(picked) <= craigslist.MAX_CATEGORIES
    assert picked[-1] == craigslist.DEFAULT_CATEGORY
    assert len(set(picked)) == len(picked)
    assert all(c in _LIVE_CATEGORIES and c not in _NEVER for c in picked)
    assert not any(re.search(craigslist.FEE_LABEL, c) for c in picked)


# --- reading a post as a visitor -----------------------------------------------------------------


def test_a_flagged_post_reads_as_flagged() -> None:
    page = "<p>This posting has been flagged for removal. [?]</p>"
    assert craigslist.post_state(410, page) == craigslist.POST_FLAGGED


def test_a_post_its_author_deleted_reads_as_gone() -> None:
    page = "This posting has been deleted by its author."
    assert craigslist.post_state(410, page) == craigslist.POST_GONE
    assert craigslist.post_state(404, "") == craigslist.POST_GONE


@given(body=st.text(max_size=200))
def test_only_craigslists_own_words_or_a_gone_status_remove_a_post(body) -> None:
    assume_plain = not re.search(r"flagged for removal|deleted by its author|expired", body, re.I)
    if assume_plain:
        assert craigslist.post_state(200, body) == craigslist.POST_LIVE
        assert craigslist.post_state(503, body) == craigslist.POST_UNKNOWN


# --- the lane ------------------------------------------------------------------------------------


def _no_rail():
    raise RailUnprovisioned("carousell.ai is not provisioned")


@pytest.fixture
def crosslisting(store, monkeypatch):
    monkeypatch.setattr("sellee.browser.formfill.sleep", lambda _seconds: None)
    monkeypatch.setattr("tests.test_craigslist_driver._CATEGORIES", _LIVE_CATEGORIES)
    store.set_seller_config_section("basics", {"region": "US", "zip": "94103"})
    made = store.create_item(
        title=_TITLE, list_price=90.0, currency="USD", description="Barely used."
    )
    seed_setting(store, "connected_markets", ["craigslist"])
    store.record_listing_url(made["id"], "carousell-ai", _RAIL_URL)
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()
    store.activate_craigslist_account("ready")
    for notice in store.claim_queued_notices(10):
        store.mark_notice_delivered(notice["id"], "channel")
    return store.get_item(made["id"])


class _Page:
    """What the post's public page says, and how often it was read."""

    def __init__(self, state=craigslist.POST_LIVE):
        self.state = state
        self.reads: list = []

    def __call__(self, url: str) -> str:
        self.reads.append(url)
        return self.state


def _deps(store, bus, form, page, now=None):
    return crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: form,
        rail_factory=_no_rail,
        fetch_post=page,
        now=now or time.time,
    )


def _notices(store) -> list:
    claimed = store.claim_queued_notices(10)
    for notice in claimed:
        store.mark_notice_delivered(notice["id"], "channel")
    return [n["text"] for n in claimed]


def _categories(store, item_id: str) -> list:
    rows = [r for r in store.publish_pass_index() if r["item_id"] == item_id]
    return [r.get("category") for r in rows if r["market"] == "craigslist"]


def test_the_first_post_goes_in_the_best_category_and_is_ledgered(store, bus, crosslisting):
    form = FakeForm()

    crosslist.enqueue_next(_deps(store, bus, form, _Page()))

    assert form.published_with["cat"] == "electronics"
    assert _categories(store, crosslisting["id"]) == ["electronics"]


def test_a_flagged_post_is_said_and_posted_again_in_the_next_category(store, bus, crosslisting):
    crosslist.enqueue_next(_deps(store, bus, FakeForm(), _Page()))
    _notices(store)
    flagged = _Page(craigslist.POST_FLAGGED)

    craigslist_posts.check(store, bus, store.get_item(crosslisting["id"]), flagged)

    assert "craigslist" not in store.get_item(crosslisting["id"])["listing_urls"]
    assert _notices(store) == [
        craigslist_posts.REPOST_NOTICE.format(title=_TITLE, was="electronics", next="cell phones")
    ]
    form = FakeForm()
    later = time.time() + crosslist.PUBLISH_RETRY_AFTER_SEC + 1
    crosslist.enqueue_next(_deps(store, bus, form, _Page(), now=lambda: later))
    assert form.published_with["cat"] == "cell phones"
    assert store.get_item(crosslisting["id"])["listing_urls"]["craigslist"]


def test_with_every_category_tried_a_flagged_post_is_retired(store, bus, crosslisting):
    for category in craigslist.categories_for(crosslisting):
        store.record_driven_publish(
            crosslisting["id"], "craigslist", status="done", origin="crosslist", category=category
        )
    store.record_listing_url(crosslisting["id"], "craigslist", _POST)
    _notices(store)

    craigslist_posts.check(
        store, bus, store.get_item(crosslisting["id"]), _Page(craigslist.POST_FLAGGED)
    )

    assert _notices(store) == [craigslist_posts.FLAGGED_NOTICE.format(title=_TITLE)]
    assert crosslist.pending_pairs(_deps(store, bus, FakeForm(), _Page()), None) == []


def test_a_post_gone_another_way_is_retired_not_posted_again(store, bus, crosslisting):
    crosslist.enqueue_next(_deps(store, bus, FakeForm(), _Page()))
    _notices(store)

    craigslist_posts.check(
        store, bus, store.get_item(crosslisting["id"]), _Page(craigslist.POST_GONE)
    )

    assert _notices(store) == [craigslist_posts.GONE_NOTICE.format(title=_TITLE)]
    later = time.time() + crosslist.PUBLISH_RETRY_AFTER_SEC + 1
    form = FakeForm()
    crosslist.enqueue_next(_deps(store, bus, form, _Page(), now=lambda: later))
    assert not form.published


@pytest.mark.parametrize("state", [craigslist.POST_LIVE, craigslist.POST_UNKNOWN])
def test_a_post_that_reads_up_or_unreadable_is_left_alone(store, bus, crosslisting, state):
    store.record_listing_url(crosslisting["id"], "craigslist", _POST)

    craigslist_posts.check(store, bus, store.get_item(crosslisting["id"]), _Page(state))

    assert store.get_item(crosslisting["id"])["listing_urls"]["craigslist"] == _POST
    assert _notices(store) == []


def test_a_sold_items_removed_post_is_dropped_without_a_word(store, bus, crosslisting, monkeypatch):
    store.record_listing_url(crosslisting["id"], "craigslist", _POST)
    monkeypatch.setattr(store, "sold_item_ids", lambda: {crosslisting["id"]})

    craigslist_posts.check(
        store, bus, store.get_item(crosslisting["id"]), _Page(craigslist.POST_FLAGGED)
    )

    assert "craigslist" not in store.get_item(crosslisting["id"])["listing_urls"]
    assert _notices(store) == []


def test_a_post_is_read_back_on_a_backoff_one_read_per_check(store, bus, crosslisting):
    crosslist.enqueue_next(_deps(store, bus, FakeForm(), _Page()))
    posted = time.time()
    page, checked = _Page(), {}

    def tick(after: float) -> int:
        craigslist_posts.sweep(store, bus, checked, posted + after, page)
        return len(page.reads)

    assert tick(30) == 0
    assert tick(61) == 1
    assert tick(120) == 1
    assert tick(301) == 2
    assert tick(1801) == 3
    assert tick(3 * 3600 + 1) == 4
    assert tick(48 * 3600) == 4


# --- where a post is about to be used ------------------------------------------------------------


@pytest.fixture
def seller(store, monkeypatch):
    seed_setting(store, "connected_markets", ["craigslist"])
    sold: set = set()
    monkeypatch.setattr(store, "sold_item_ids", lambda: set(sold))
    store.sold = sold
    return store


def test_open_on_desktop_on_a_removed_post_opens_nothing_and_says_so(seller, bus, monkeypatch):
    item = _sold(seller, craigslist=_POST)
    _tap(seller, bus, item["id"])
    chrome = _Chrome()
    monkeypatch.setattr(craigslist_posts, "fetch_state", lambda _url: craigslist.POST_GONE)

    _lane(seller, bus, chrome, monkeypatch)

    assert chrome.opened == []
    assert seller.pending_post_opens() == []
    texts = [n["text"] for n in seller.list_queued_notices()]
    assert texts[-1] == "That Craigslist post is already down, so there's nothing to delete."


def test_an_edit_to_a_removed_post_is_refused_and_drives_nothing(store, bus, monkeypatch):
    seed_setting(store, "connected_markets", ["craigslist"])
    made = store.create_item(title="Samsung Buds3 Pro", list_price=65.0, currency="USD")
    store.record_listing_url(made["id"], "craigslist", _POST)
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()
    store.activate_craigslist_account("ready")
    rev = store.queue_listing_revision(made["id"], "craigslist", ["list_price"])
    post = FakePost()
    deps = revise.ReviseDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: post,
        fetch_post=_Page(craigslist.POST_FLAGGED),
    )

    revise.run_next(deps)

    row = store.get_listing_revision(rev)
    assert row["status"] == "failed" and "taken the post down" in row["last_error"]
    assert post.navigated == []
