"""The Craigslist driver walks the posting form a `?s=` step at a time, presses publish only on a
preview that shows the item, and returns only the post address buyer mail names."""

from __future__ import annotations

import contextlib
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting

from sellee import crosslist, marketplaces
from sellee.browser import markets as market_adapters
from sellee.browser import publisher
from sellee.browser.markets import craigslist
from sellee.config import Config
from sellee.rail import registration
from sellee.rail.client import RailUnprovisioned

_POST = "https://www.craigslist.org/view/d/samsung-buds3/9TXkjafxYMDS5VaPLGdRyk"
_MANAGE = "https://post.craigslist.org/manage/9TXkjafxYMDS5VaPLGdRyk"
_RAIL_URL = "https://www.carousell.ai/listing/abc123"
_SF = ["subarea", "hood", "type", "cat", "edit", "geoverify", "editimage", "preview"]
_BOZEMAN = ["type", "cat", "edit", "geoverify", "editimage", "preview"]
_AREAS = ["city of san francisco", "south bay area", "east bay area"]
_CATEGORIES = ["computer parts", "cars & trucks ($5)", "general for sale", "rvs ($5)"]
_ITEM = {"id": "i1", "title": "Samsung Buds3", "list_price": 80.0, "description": "Barely used."}


class FakeForm:
    """Craigslist's posting form as the spike recorded it, one `?s=` step per page."""

    def __init__(
        self,
        steps=tuple(_BOZEMAN),
        *,
        lag=0,
        signed_out_at=None,
        chat_sticks=False,
        mangle=None,
        confirms=True,
        post_url=_POST,
        site="bozeman",
    ):
        self.steps = list(steps) + ["confirmed"]
        self.at = 0
        self.lag = lag
        self.pending = 0
        self.signed_out_at = signed_out_at
        self.chat_on = True
        self.chat_sticks = chat_sticks
        self.mangle = mangle or {}
        self.confirms = confirms
        self.post_url = post_url
        self.site = site
        self.fields: dict = {}
        self.marked: str | None = None
        self.chosen: dict = {}
        self.images = 0
        self.published = False
        self.published_with: dict = {}
        self.read_back_before_publish = False
        self.navigated: list = []

    @property
    def step(self) -> str:
        return self.steps[min(self.at, len(self.steps) - 1)]

    @contextlib.contextmanager
    def exclusive(self):
        yield

    def navigate_visible(self, url: str) -> None:
        self.navigated.append(url)
        self.at = 0

    def navigate(self, url: str) -> None:
        self.navigated.append(url)
        if url == _MANAGE:
            self.steps.append("manage")
            self.at = len(self.steps) - 1

    def _advance(self) -> None:
        self.at += 1
        self.pending = self.lag
        self.marked = None

    def evaluate(self, function: str, **_):
        if function == craigslist.STEP_JS:
            shown = self.step
            if self.pending:
                self.pending -= 1
                shown = self.steps[self.at - 1]
            return {
                "step": shown,
                "site": self.site,
                "logged_in": self.signed_out_at != shown,
                "images": self.images if shown == "editimage" else None,
            }
        if function == craigslist.HOOD_BYPASS_JS:
            self.marked = "bypass"
            return {"chosen": True}
        if "const wanted" in function:
            wanted = re.search(r"const wanted = '([^']*)'", function).group(1)
            options = {"subarea": _AREAS, "type": ["for sale by owner"], "cat": _CATEGORIES}
            offered = options.get(self.step, [])
            self.marked = wanted if wanted in offered else None
            return {"chosen": self.marked is not None, "options": offered}
        if function == craigslist.EDIT_READBACK_JS:
            seen = {
                "title": self.fields.get(craigslist.TITLE),
                "price": self.fields.get(craigslist.PRICE),
                "zip": self.fields.get(craigslist.ZIP),
                "chat_on": self.chat_on,
            }
            seen.update(self.mangle)
            return seen
        if function == craigslist.PREVIEW_TEXT_JS:
            self.read_back_before_publish = True
            title = self.mangle.get("title", self.fields.get(craigslist.TITLE))
            return {"text": f"{title} - ${self.fields.get(craigslist.PRICE)} (Downtown)"}
        if function == craigslist.MANAGE_LINK_JS:
            return {"url": _MANAGE}
        if function == craigslist.MANAGED_POST_JS:
            return {"url": self.post_url, "post_id": "7978162581"}
        raise AssertionError(f"unexpected script: {function[:60]}")

    def type_humanly(self, target: str, element: str, text: str) -> None:
        self.fields[target] = text

    def call_tool(self, name: str, arguments: dict):
        if name == "browser_file_upload":
            self.images += len(arguments["paths"])
        return ""

    def click(self, target: str, element: str) -> None:
        if target == craigslist.CHOICE:
            assert self.marked is not None, "clicked a choice that was never marked"
            self.chosen[self.step] = self.marked
        elif target == craigslist.CHAT:
            if not self.chat_sticks:
                self.chat_on = not self.chat_on
        elif target == craigslist.PUBLISH:
            assert self.step == "preview"
            self.published = True
            self.published_with = {"chat_on": self.chat_on, "cat": self.chosen.get("cat")}
            if self.confirms:
                self._advance()
        elif target in (craigslist.CONTINUE, craigslist.MAP_CONTINUE, craigslist.DONE_WITH_IMAGES):
            self._advance()


def _seller(**more) -> dict:
    return {"region": "US", "zip": "94103", **more}


def _drive(form, item=_ITEM, seller=None, photos=()):
    return publisher.publish(
        form,
        market_adapters.get_adapter("craigslist"),
        item,
        create_url=craigslist.POST_URL,
        photos=photos,
        seller=_seller() if seller is None else seller,
        sleep=lambda _s: None,
    )


def _footer(url: str) -> str:
    return f"Hi, is this available?\n------\nOriginal craigslist post:\n{url}\nAbout craigslist:"


# --- the drive ----------------------------------------------------------------------------------


def test_a_small_site_is_posted_and_its_view_url_returned() -> None:
    form = FakeForm()

    outcome = _drive(form)

    assert outcome.verified and outcome.url == _POST and outcome.listing_id == "7978162581"
    assert form.chosen == {"type": "for sale by owner", "cat": craigslist.DEFAULT_CATEGORY}
    assert form.fields[craigslist.ZIP] == "94103" and form.fields[craigslist.PRICE] == "80"
    assert form.published_with["chat_on"] is False
    assert form.navigated == [craigslist.POST_URL, _MANAGE]


def test_a_multi_area_site_takes_the_sellers_area_and_bypasses_the_neighborhood() -> None:
    form = FakeForm(_SF, site="SF bay area")

    outcome = _drive(form, seller=_seller(craigslist_area="east bay area"))

    assert outcome.verified
    assert form.chosen["subarea"] == "east bay area" and form.chosen["hood"] == "bypass"


@pytest.mark.parametrize("area", [None, "brooklyn"])
def test_an_area_craigslist_does_not_offer_asks_the_seller_with_its_choices(area) -> None:
    form = FakeForm(_SF, site="SF bay area")
    seller = _seller() if area is None else _seller(craigslist_area=area)

    with pytest.raises(publisher.PublishNeedsSeller) as raised:
        _drive(form, seller=seller)

    assert raised.value.key == "craigslist_area"
    assert "SF bay area" in raised.value.question
    assert all(label in raised.value.question for label in _AREAS)
    assert not form.published


def test_pages_that_load_after_the_click_returns_are_waited_for() -> None:
    assert _drive(FakeForm(lag=3)).verified


def test_photos_are_uploaded_before_moving_on() -> None:
    form = FakeForm()

    assert _drive(form, photos=["/tmp/01.jpg", "/tmp/02.jpg"]).verified
    assert form.images == 2


def test_a_post_craigslist_does_not_confirm_is_unverified_and_names_nothing() -> None:
    outcome = _drive(FakeForm(confirms=False))

    assert not outcome.verified and outcome.url == ""


def test_a_regional_post_address_is_never_returned_as_the_listing() -> None:
    regional = "https://sfbay.craigslist.org/sfc/sop/d/samsung-buds3/7978162581.html"

    outcome = _drive(FakeForm(post_url=regional))

    assert not outcome.verified and outcome.url == ""


@pytest.mark.parametrize("label", ["cars & trucks ($5)", "rvs ($5)", "motorcycles/scooters ($5)"])
def test_a_category_that_costs_money_is_refused(monkeypatch, label) -> None:
    monkeypatch.setattr(craigslist, "DEFAULT_CATEGORY", label)
    form = FakeForm()

    with pytest.raises(publisher.PublishNotAttempted):
        _drive(form)

    assert not form.published


_DISTURBANCES = st.fixed_dictionaries(
    {
        "steps": st.sampled_from([_SF, _BOZEMAN, ["type", "cat", "mystery", "edit", "preview"]]),
        "lag": st.integers(0, 50),
        "signed_out_at": st.sampled_from([None, "type", "edit", "preview"]),
        "chat_sticks": st.booleans(),
        "mangle": st.sampled_from([{}, {"title": "Samsung Bud"}, {"price": "8"}, {"zip": "9410"}]),
        "confirms": st.booleans(),
        "post_url": st.sampled_from([_POST, "", "https://sfbay.craigslist.org/sfc/sop/d/x/1.html"]),
    }
)


@settings(max_examples=200, deadline=None)
@given(disturbance=_DISTURBANCES, area=st.sampled_from([None, "east bay area", "nowhere"]))
def test_publish_is_pressed_only_on_a_preview_that_matches(disturbance, area) -> None:
    form = FakeForm(**disturbance)
    seller = _seller() if area is None else _seller(craigslist_area=area)

    try:
        outcome = _drive(form, seller=seller)
    except publisher.PublishNotAttempted:
        assert not form.published
        return
    except publisher.PublishUnverified:
        assert form.published
        return

    assert form.published and form.read_back_before_publish
    assert form.published_with["chat_on"] is False
    assert not re.search(craigslist.FEE_LABEL, form.published_with["cat"] or "")
    assert not disturbance["mangle"] and disturbance["signed_out_at"] != "preview"
    if outcome.verified:
        assert marketplaces.is_canonical_listing_url("craigslist", outcome.url)
        assert registration.last_footer_url(_footer(outcome.url)) == outcome.url
    else:
        assert outcome.url == ""


# --- the crosslist lane drives it ---------------------------------------------------------------


def _no_rail():
    raise RailUnprovisioned("carousell.ai is not provisioned")


@pytest.fixture
def crosslisting(store, monkeypatch):
    monkeypatch.setattr("sellee.browser.formfill.sleep", lambda _seconds: None)
    store.set_seller_config_section("basics", {"region": "US", "zip": "94103"})
    made = store.create_item(title="Samsung Buds3", list_price=80.0, currency="USD")
    seed_setting(store, "connected_markets", ["craigslist"])
    store.record_listing_url(made["id"], "carousell-ai", _RAIL_URL)
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()
    store.activate_craigslist_account("ready")
    for notice in store.claim_queued_notices(10):
        store.mark_notice_delivered(notice["id"], "channel")
    return store.get_item(made["id"])


def _deps(store, bus, form):
    return crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=lambda: form,
        rail_factory=_no_rail,
    )


def _notices(store) -> list:
    return [n["text"] for n in store.claim_queued_notices(10)]


def test_a_ready_post_is_driven_and_its_url_recorded(store, bus, crosslisting) -> None:
    form = FakeForm()

    crosslist.enqueue_next(_deps(store, bus, form))

    assert form.published
    assert store.get_item(crosslisting["id"])["listing_urls"]["craigslist"] == _POST
    assert store.publish_pass_index() == [] or all(
        row["status"] != "queued" for row in store.publish_pass_index()
    )


def test_a_missing_area_asks_once_and_waits_for_the_answer(store, bus, crosslisting) -> None:
    form = FakeForm(_SF, site="SF bay area")
    deps = _deps(store, bus, form)

    crosslist.enqueue_next(deps)
    crosslist.enqueue_next(deps)

    asked = _notices(store)
    assert len(asked) == 1 and "east bay area" in asked[0]
    assert form.navigated == [craigslist.POST_URL]
    assert "craigslist" not in store.get_item(crosslisting["id"])["listing_urls"]

    store.set_seller_config_section(
        "basics", {"region": "US", "zip": "94103", "craigslist_area": "east bay area"}
    )
    crosslist.enqueue_next(deps)

    assert store.get_item(crosslisting["id"])["listing_urls"]["craigslist"] == _POST
