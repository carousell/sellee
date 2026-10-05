"""A sold item's Craigslist post is closed by the seller, on the page sellee's Chrome opens for
them: the take-down notice carries a button that opens the post's manage page."""

from __future__ import annotations

import contextlib

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting

from sellee.browser import connect
from sellee.channel import fastpaths
from sellee.config import Config
from sellee.tools import listing

_TOKEN = "9TXkjafxYMDS5VaPLGdRyk"
_POST = f"https://www.craigslist.org/view/d/samsung-buds3/{_TOKEN}"
_FB = "https://www.facebook.com/marketplace/item/123/"


def _sold(store, **urls) -> dict:
    made = store.create_item(title="Samsung Buds3", list_price=80.0, currency="USD")
    for market, url in urls.items():
        store.record_listing_url(made["id"], market, url)
    return made


def _tap(store, bus, item_id: str) -> tuple:
    event = {
        "kind": "action",
        "text": fastpaths.CB_OPEN_POST,
        "payload": {"ref": item_id, "choice": fastpaths.CB_OPEN_POST},
    }
    return fastpaths.handle_fast_path(store, bus, event)


def _queued(store) -> list:
    return store.list_queued_notices()


# --- the button ----------------------------------------------------------------------------------


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    slug=st.from_regex(r"[a-z0-9]{1,10}(-[a-z0-9]{1,10}){0,3}", fullmatch=True),
    token=st.from_regex(r"[A-Za-z0-9]{8,24}", fullmatch=True),
)
def test_the_button_opens_the_manage_page_of_the_recorded_post(store, bus, slug, token) -> None:
    with store._db.transaction() as conn:
        conn.execute("DELETE FROM market_connect_requests")
    item = _sold(store, craigslist=f"https://www.craigslist.org/view/d/{slug}/{token}")

    _tap(store, bus, item["id"])

    [request] = store.pending_market_connects()
    assert request["url"] == f"https://post.craigslist.org/manage/{token}"


def test_a_tap_for_an_item_with_no_craigslist_post_opens_nothing(store, bus) -> None:
    item = _sold(store, fb=_FB)

    text, _controls = _tap(store, bus, item["id"])

    assert text == fastpaths.OPEN_POST_GONE
    assert store.pending_market_connects() == []


# --- the notice ----------------------------------------------------------------------------------


def test_a_sold_craigslist_post_gets_a_notice_with_open_on_desktop(store, make_ctx) -> None:
    item = _sold(store, craigslist=_POST)

    listing._manual_take_downs(make_ctx("attended"), item["id"])

    [notice] = _queued(store)
    assert "Delete this Posting" in notice["text"] and _POST in notice["text"]
    assert fastpaths.OPEN_POST_LABEL in str(notice)


def test_notices_for_other_browser_markets_are_unchanged(store, make_ctx) -> None:
    item = _sold(store, fb=_FB)

    listing._manual_take_downs(make_ctx("attended"), item["id"])

    [notice] = _queued(store)
    assert notice["text"] == listing.MANUAL_TAKE_DOWN_NOTICE.format(
        market="Facebook Marketplace", url=_FB
    )
    assert fastpaths.OPEN_POST_LABEL not in str(notice)


# --- the connect lane ----------------------------------------------------------------------------


class _Chrome:
    def __init__(self):
        self.opened: list = []

    @contextlib.contextmanager
    def exclusive(self):
        yield

    def navigate_visible(self, url: str) -> None:
        self.opened.append(url)


def test_the_lane_opens_the_post_raises_chrome_and_says_what_to_press(
    store, bus, monkeypatch
) -> None:
    raised = []
    monkeypatch.setattr(connect, "_raise_window", lambda deps: raised.append(True))
    seed_setting(store, "connected_markets", ["craigslist"])
    item = _sold(store, craigslist=_POST)
    _tap(store, bus, item["id"])
    chrome = _Chrome()

    connect.connect_lane(
        connect.ConnectDeps(store=store, bus=bus, config=Config(), browser_factory=lambda: chrome)
    )

    assert chrome.opened == [f"https://post.craigslist.org/manage/{_TOKEN}"]
    assert raised == [True]
    assert store.pending_market_connects() == []
    assert any("Delete this Posting" in n["text"] for n in _queued(store))
