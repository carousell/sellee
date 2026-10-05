"""A sold item's Craigslist post is closed by the seller, on the page sellee's Chrome opens for
them: the take-down notice carries a button that opens the post's manage page."""

from __future__ import annotations

import contextlib
import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting

from sellee.browser import connect
from sellee.browser.markets import craigslist
from sellee.channel import fastpaths
from sellee.config import Config
from sellee.tools import listing

_TOKEN = "9TXkjafxYMDS5VaPLGdRyk"
_POST = f"https://www.craigslist.org/view/d/samsung-buds3/{_TOKEN}"
_MANAGE = f"https://post.craigslist.org/manage/{_TOKEN}"
_FB = "https://www.facebook.com/marketplace/item/123/"


@pytest.fixture
def seller(store, monkeypatch):
    seed_setting(store, "connected_markets", ["craigslist", "fb"])
    sold: set = set()
    monkeypatch.setattr(store, "sold_item_ids", lambda: set(sold))
    store.sold = sold
    return store


def _sold(store, **urls) -> dict:
    made = store.create_item(title="Samsung Buds3", list_price=80.0, currency="USD")
    for market, url in urls.items():
        store.record_listing_url(made["id"], market, url)
    store.sold.add(made["id"])
    return made


def _tap(store, bus, item_id: str) -> tuple:
    event = {
        "kind": "action",
        "text": fastpaths.CB_OPEN_POST,
        "payload": {"ref": item_id, "choice": fastpaths.CB_OPEN_POST},
    }
    return fastpaths.handle_fast_path(store, bus, event)


def _texts(store) -> list:
    return [n["text"] for n in store.list_queued_notices()]


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
def test_the_button_opens_the_manage_page_of_the_recorded_post(seller, bus, slug, token) -> None:
    item = _sold(seller, craigslist=f"https://www.craigslist.org/view/d/{slug}/{token}")

    _tap(seller, bus, item["id"])

    [request] = [r for r in seller.pending_post_opens() if r["item_id"] == item["id"]]
    assert request["url"] == f"https://post.craigslist.org/manage/{token}"


def test_a_tap_for_an_item_with_no_craigslist_post_opens_nothing(seller, bus) -> None:
    item = _sold(seller, fb=_FB)

    assert _tap(seller, bus, item["id"])[0] == fastpaths.OPEN_POST_GONE
    assert seller.pending_post_opens() == []


def test_a_tap_for_an_item_no_longer_sold_leaves_its_post_alone(seller, bus) -> None:
    item = _sold(seller, craigslist=_POST)
    seller.sold.discard(item["id"])

    assert _tap(seller, bus, item["id"])[0] == fastpaths.OPEN_POST_NOT_SOLD
    assert seller.pending_post_opens() == []


def test_an_open_post_tap_leaves_a_pending_sign_in_alone(seller, bus) -> None:
    seller.request_market_connect("craigslist")
    first, second = _sold(seller, craigslist=_POST), _sold(seller, craigslist=_POST + "x")

    _tap(seller, bus, first["id"])
    _tap(seller, bus, second["id"])

    assert [r["market"] for r in seller.pending_market_connects()] == ["craigslist"]
    assert len(seller.pending_post_opens()) == 2


# --- the notice ----------------------------------------------------------------------------------


def test_a_sold_craigslist_post_gets_a_notice_with_open_on_desktop(seller, make_ctx) -> None:
    item = _sold(seller, craigslist=_POST)

    listing._manual_take_downs(make_ctx("attended"), item["id"])

    [notice] = seller.list_queued_notices()
    assert "Delete this Posting" in notice["text"] and _POST in notice["text"]
    assert [tuple(pair) for pair in notice["controls"]] == [
        (fastpaths.OPEN_POST_LABEL, f"{item['id']}:{fastpaths.CB_OPEN_POST}")
    ]


def test_notices_for_other_browser_markets_are_unchanged(seller, make_ctx) -> None:
    item = _sold(seller, fb=_FB)

    listing._manual_take_downs(make_ctx("attended"), item["id"])

    [notice] = seller.list_queued_notices()
    assert notice["text"] == listing.MANUAL_TAKE_DOWN_NOTICE.format(
        market="Facebook Marketplace", url=_FB
    )
    assert not notice["controls"]


# --- the connect lane ----------------------------------------------------------------------------


class _Chrome:
    def __init__(self, signed_in=True):
        self.opened: list = []
        self.signed_in = signed_in

    @contextlib.contextmanager
    def exclusive(self):
        yield

    def navigate_visible(self, url: str) -> None:
        self.opened.append(url)

    def evaluate(self, function: str, **_):
        assert function == craigslist.LOGIN_JS
        return {"state": "logged_in" if self.signed_in else "logged_out"}


def _lane(store, bus, chrome, monkeypatch, raised=None, now=None) -> None:
    monkeypatch.setattr(
        connect, "_raise_window", lambda deps: (raised if raised is not None else []).append(True)
    )
    connect.connect_lane(
        connect.ConnectDeps(
            store=store,
            bus=bus,
            config=Config(),
            browser_factory=lambda: chrome,
            now=now or time.time,
        )
    )


def _done(store, bus, item_id: str) -> tuple:
    event = {
        "kind": "action",
        "text": fastpaths.CB_POST_DONE,
        "payload": {"ref": item_id, "choice": fastpaths.CB_POST_DONE},
    }
    return fastpaths.handle_fast_path(store, bus, event)


def test_the_lane_opens_the_post_holds_the_tab_and_says_what_to_press(
    seller, bus, monkeypatch
) -> None:
    item = _sold(seller, craigslist=_POST)
    _tap(seller, bus, item["id"])
    chrome, raised = _Chrome(), []

    _lane(seller, bus, chrome, monkeypatch, raised)

    assert chrome.opened == [_MANAGE] and raised == [True]
    assert seller.pending_post_opens() == []
    assert seller.browser_hold_reason() == "closing a craigslist post"
    assert any("Delete this Posting" in text for text in _texts(seller))
    opened = next(n for n in seller.list_queued_notices() if "Delete this Posting" in n["text"])
    assert [tuple(c) for c in opened["controls"]] == fastpaths.post_done_controls(item["id"])


def test_two_taps_open_one_post_then_the_next_after_done(seller, bus, monkeypatch) -> None:
    first = _sold(seller, craigslist=_POST)
    second = _sold(seller, craigslist=_POST.replace(_TOKEN, "SecondPostToken12345"))
    _tap(seller, bus, first["id"])
    _tap(seller, bus, second["id"])
    chrome = _Chrome()

    _lane(seller, bus, chrome, monkeypatch)
    later = time.time() + connect.STALE_REQUEST_SEC + 60
    _lane(seller, bus, chrome, monkeypatch, now=lambda: later)

    # Waiting behind the first post's hold is not stale, however long it takes.
    assert chrome.opened == [_MANAGE]
    assert [r["item_id"] for r in seller.pending_post_opens()] == [second["id"]]
    assert connect.POST_STALE_NOTICE not in _texts(seller)

    assert _done(seller, bus, first["id"]) == (fastpaths.POST_DONE_ACK, None)
    _lane(seller, bus, chrome, monkeypatch, now=lambda: later)

    assert chrome.opened == [_MANAGE, "https://post.craigslist.org/manage/SecondPostToken12345"]
    assert seller.pending_post_opens() == []


def test_done_frees_only_its_own_post(seller, bus, monkeypatch) -> None:
    item = _sold(seller, craigslist=_POST)
    _tap(seller, bus, item["id"])
    _lane(seller, bus, _Chrome(), monkeypatch)

    _done(seller, bus, "some-other-item")

    assert seller.browser_hold_reason() == "closing a craigslist post"
    _done(seller, bus, item["id"])
    assert seller.browser_hold_reason() == ""


def test_a_request_behind_a_sign_in_still_goes_stale(seller, bus, monkeypatch) -> None:
    item = _sold(seller, craigslist=_POST)
    _tap(seller, bus, item["id"])
    seller.hold_browser("signin", "signing in to fb", 3600)
    later = time.time() + connect.STALE_REQUEST_SEC + 60

    _lane(seller, bus, _Chrome(), monkeypatch, now=lambda: later)

    assert connect.POST_STALE_NOTICE in _texts(seller)
    assert seller.pending_post_opens() == []


def test_a_signed_out_page_is_reported_not_called_open(seller, bus, monkeypatch) -> None:
    item = _sold(seller, craigslist=_POST)
    _tap(seller, bus, item["id"])

    _lane(seller, bus, _Chrome(signed_in=False), monkeypatch)

    assert connect.POST_SIGNED_OUT_NOTICE in _texts(seller)
    assert not any("Delete this Posting" in text for text in _texts(seller))


def test_a_market_that_asked_us_to_stop_opens_nothing(seller, bus, monkeypatch) -> None:
    item = _sold(seller, craigslist=_POST)
    _tap(seller, bus, item["id"])
    monkeypatch.setattr(seller, "market_block", lambda market: {"cause": "checkpoint"})
    chrome = _Chrome()

    _lane(seller, bus, chrome, monkeypatch)

    assert chrome.opened == [] and connect.POST_BLOCKED_NOTICE in _texts(seller)


def test_craigslist_switched_off_after_the_tap_is_said(seller, bus, monkeypatch) -> None:
    item = _sold(seller, craigslist=_POST)
    _tap(seller, bus, item["id"])
    seed_setting(seller, "connected_markets", ["fb"])

    _lane(seller, bus, _Chrome(), monkeypatch)

    assert connect.POST_OFF_NOTICE in _texts(seller)
    assert seller.pending_post_opens() == []
