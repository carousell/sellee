"""What adoption actually buys, proved against the lanes that were already there.

The whole design rests on one claim: taking over a listing is nothing more than writing item rows,
and every existing lane then applies with no special path. These tests hold that claim to the two
lanes it matters for — the read lane, which today drops a conversation about a listing it does not
recognise, and the fan-out, which lists an item everywhere else the seller sells.

If adoption ever stops producing exactly the rows those lanes key on, this is what says so.
"""

from __future__ import annotations

import pytest
from tests.test_browser_inbox import StubClient as InboxStub
from tests.test_browser_inbox import _conv
from tests.test_browser_inbox import _deps as _inbox_deps
from tests.test_market_survey import StubClient as SurveyStub
from tests.test_market_survey import _accepted, _deps, _detail, _fetches, _listing, _media_photo

from sellee import crosslist
from sellee.browser import adopt, inbox
from sellee.config import Config

_MARKET = "carousell"
# The survey's own listing id, and the URL an adopted item ends up recording.
_LISTING_ID = "111"
_LISTING_URL = f"https://www.carousell.sg/p/teak-lamp-{_LISTING_ID}/"


@pytest.fixture(autouse=True)
def _one_market(carousell_only):
    """Carousell alone — a lane tick drives every connected market, and these script Carousell's
    artifacts only."""


def _adopt_one(store, bus, monkeypatch):
    _accepted(store, bus, [_listing(_LISTING_ID, "Teak lamp")])
    _fetches(monkeypatch, [_media_photo()])
    adopt.adopt_phase(_deps(store, bus, SurveyStub(detail=_detail())))
    return store.list_discovered_listings(_MARKET)[0]


def test_an_adopted_listing_makes_the_read_lane_adopt_its_buyers(
    store, bus, monkeypatch, xdg_tmp
) -> None:
    """Today a buyer writing about a listing the seller made themselves is dropped as
    `unknown_listing` — that is the gap this feature exists to close."""
    row = _adopt_one(store, bus, monkeypatch)
    assert store.get_item(row["item_id"])["listing_urls"][_MARKET] == _LISTING_URL

    client = InboxStub(
        conversations=[_conv(product_id=_LISTING_ID)],
        tails={"99": [{"text": "still available?", "side": "in", "y": 0}]},
    )
    inbox.inbox_lane(_inbox_deps(store, bus, client))

    threads = store.list_threads(side="sell")
    assert [t["item_id"] for t in threads] == [row["item_id"]]
    assert bus.store.read(kinds=["browser.thread_new"])
    assert not bus.store.read(kinds=["browser.unmatched"])
    messages = store.get_thread_messages(threads[0]["thread_id"], limit=None)
    assert [m["text"] for m in messages] == ["still available?"]


def test_the_fan_out_holds_an_adopted_item_until_its_rail_listing_exists(
    store, bus, monkeypatch, xdg_tmp
) -> None:
    """Rail-first is the fan-out's precondition, and an adopted item is subject to it like any
    other. It is also never fanned back to the marketplace it came from, which would put a second
    live listing on the seller's own account."""
    from tests.conftest import seed_setting

    row = _adopt_one(store, bus, monkeypatch)
    seed_setting(store, "connected_markets", [_MARKET])
    deps = crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: object(),
        rail_factory=lambda: None,
    )

    assert crosslist.pending_pairs(deps) == [], "no carousell.ai listing yet, so nothing to fan out"

    store.record_listing_url(row["item_id"], "carousell-ai", "https://carousell.ai/listing/abc")

    assert [market for _item, market in crosslist.pending_pairs(deps)] == [], (
        "the marketplace it was adopted from already has it — publishing again would leave the "
        "seller with two live listings for one thing"
    )


def test_an_adopted_listing_is_linked_from_its_rail_listing(store, bus, monkeypatch, xdg_tmp):
    """The other direction of the fan-out: the cross-link push renders "Also available on" to
    buyers, and it derives from where the item actually is — so an adopted marketplace URL earns
    the link with no wiring of its own."""
    row = _adopt_one(store, bus, monkeypatch)
    store.record_listing_url(row["item_id"], "carousell-ai", "https://carousell.ai/listing/abc")

    desired = crosslist.desired_external_urls(store.get_item(row["item_id"])["listing_urls"])

    assert desired == [{"platform": "EXTERNAL_PLATFORM_CAROUSELL", "url": _LISTING_URL}]
