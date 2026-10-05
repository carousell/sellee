"""Posting to Craigslist: the URL a publish records is the one buyer mail names, and the crosslist
lane holds a post until there is an account and a ZIP code to post with."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting

from sellee import crosslist
from sellee.browser import craigslist_account
from sellee.config import Config
from sellee.rail import registration
from sellee.rail.client import RailUnprovisioned
from sellee.tools.registry import ToolError, dispatch
from sellee.tools.seller import BasicsError, validate_basics

_RAIL_URL = "https://www.carousell.ai/listing/abc123"
_POST = "https://www.craigslist.org/view/d/samsung-buds3/9TXkjafxYMDS5VaPLGdRyk"
_PROPERTY = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def _footer(url: str) -> str:
    return (
        f"Hi, is this available?\n------\nOriginal craigslist post:\n{url}\nAbout craigslist mail:"
    )


@pytest.fixture
def us_item(store):
    store.set_seller_config_section("basics", {"region": "US", "zip": "94103"})
    made = store.create_item(title="Samsung Buds3", list_price=80.0, currency="USD")
    return store.get_item(made["id"])


def _record(item, make_ctx, url: str) -> str:
    out = dispatch(
        "record_published_listing_url",
        {"item_id": item["id"], "market": "craigslist", "url": url},
        make_ctx("pass:publish"),
    )
    return out["url"]


# --- the URL a publish records -------------------------------------------------------------------

_SLUGS = st.from_regex(r"[a-z0-9]{1,12}(-[a-z0-9]{1,12}){0,4}", fullmatch=True)
_TOKENS = st.from_regex(r"[A-Za-z0-9]{8,30}", fullmatch=True)


@_PROPERTY
@given(slug=_SLUGS, token=_TOKENS)
def test_a_recorded_post_url_is_the_one_buyer_mail_names(store, us_item, make_ctx, slug, token):
    url = f"https://www.craigslist.org/view/d/{slug}/{token}"

    recorded = _record(us_item, make_ctx, url)

    assert store.get_item(us_item["id"])["listing_urls"]["craigslist"] == recorded
    assert registration.last_footer_url(_footer(url)) == recorded


_REPORTED = st.one_of(
    st.text(max_size=60),
    st.builds(
        "{}{}{}{}{}".format,
        st.sampled_from(["https://", "http://", " https://", ""]),
        st.sampled_from(
            ["www.craigslist.org", "sfbay.craigslist.org", "craigslist.org", "evil.example"]
        ),
        st.sampled_from(["/view/d/", "/sfc/sop/d/", "/d/", "/view/d"]),
        st.text(alphabet="abc-/123XYZ.?#=&", max_size=30),
        st.sampled_from(["", " ", "/", "?x=1", "#top", ".html"]),
    ),
)


@_PROPERTY
@given(reported=_REPORTED)
@example(reported="https://sfbay.craigslist.org/sfc/sop/d/samsung-buds3/7978162581.html")
@example(reported="https://sfbay.craigslist.org/view/d/samsung-buds3/9TXkjafxYMDS5VaPLGdRyk")
@example(reported=_POST + "?lang=en")
@example(reported=_POST + "/")
def test_anything_recorded_is_a_url_buyer_mail_can_name(store, us_item, make_ctx, reported):
    try:
        recorded = _record(us_item, make_ctx, reported)
    except ToolError:
        assert "craigslist" not in store.get_item(us_item["id"])["listing_urls"]
        return
    assert recorded.startswith("https://www.craigslist.org/view/d/")
    assert registration.last_footer_url(_footer(recorded)) == recorded


def test_surrounding_whitespace_is_not_recorded(store, us_item, make_ctx) -> None:
    assert _record(us_item, make_ctx, f"  {_POST}\n") == _POST
    assert store.get_item(us_item["id"])["listing_urls"]["craigslist"] == _POST


# --- the seller's ZIP code -----------------------------------------------------------------------


def test_a_zip_code_is_kept_as_five_digits() -> None:
    assert validate_basics({"zip": " 94103 "}) == {"zip": "94103"}


@pytest.mark.parametrize("bad", ["9410", "941033", "SW1A 1AA", "94103-1234x", ""])
def test_a_zip_code_that_is_not_five_digits_is_refused(bad) -> None:
    with pytest.raises(BasicsError):
        validate_basics({"zip": bad})


def test_a_craigslist_area_is_kept_as_offered() -> None:
    assert validate_basics({"craigslist_area": "  city of  san francisco "}) == {
        "craigslist_area": "city of san francisco"
    }


# --- the crosslist lane's account gate -----------------------------------------------------------


def _no_rail():
    raise RailUnprovisioned("carousell.ai is not provisioned")


def _deps(store, bus):
    return crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=_no_rail,
    )


@pytest.fixture
def crosslisting(store, us_item):
    seed_setting(store, "connected_markets", ["craigslist"])
    store.record_listing_url(us_item["id"], "carousell-ai", _RAIL_URL)
    return us_item


def _active(store) -> None:
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()
    store.activate_craigslist_account("ready")
    for notice in store.claim_queued_notices(10):
        store.mark_notice_delivered(notice["id"], "channel")


def _queued_markets(store) -> list:
    return [r["market"] for r in store.publish_pass_index() if r["status"] == "queued"]


def _notices(store) -> list:
    return [n["text"] for n in store.claim_queued_notices(10)]


def test_with_no_account_a_post_asks_for_one_and_is_not_attempted(store, bus, crosslisting):
    crosslist.enqueue_next(_deps(store, bus))
    crosslist.enqueue_next(_deps(store, bus))

    assert _queued_markets(store) == []
    assert store.craigslist_account()["state"] == craigslist_account.SIGNUP_REQUESTED
    assert _notices(store) == [craigslist_account.SIGNING_UP_NOTICE]


def test_an_account_still_being_set_up_holds_the_post(store, bus, crosslisting) -> None:
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()

    crosslist.enqueue_next(_deps(store, bus))

    assert _queued_markets(store) == []


def test_without_a_zip_code_the_seller_is_asked_once(store, bus, crosslisting) -> None:
    _active(store)
    store.set_seller_config_section("basics", {"region": "US"})
    deps = _deps(store, bus)

    crosslist.enqueue_next(deps)
    crosslist.enqueue_next(deps)

    assert _queued_markets(store) == []
    assert _notices(store) == [craigslist_account.ZIP_NOTICE]
