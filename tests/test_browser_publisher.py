"""Publishing by driving the form: what it refuses to do, and what it will never do twice.

Almost every test here is a refusal, because the expensive mistakes all live on one side of a single
line. Before the commit a listing does not exist and anything wrong is free; after it, a listing may
exist and the only safe move is to stop. So what is held here is that line: which failures leave the
work retryable, which retire it, and that nothing gets pressed until the form has been read back and
agrees with what it was given.
"""

from __future__ import annotations

import pytest

from sellee.browser import markets as market_adapters
from sellee.browser import publisher
from sellee.browser.client import BrowserToolError

_CREATE = "https://www.facebook.com/marketplace/create/item"
_ADAPTER = market_adapters.FACEBOOK

_ALL_FIELDS = [
    "title",
    "price",
    "category",
    "condition",
    "description",
    "photos",
    "add_photos",
    "more",
    "next",
]


class StubForm:
    """A create form answering the publish artifacts from a script, recording every action."""

    def __init__(
        self,
        *,
        marked=None,
        after_next=None,
        readback=None,
        chosen="ok",
        boost_on=False,
        listing_id="999",
        next_enabled=True,
        fail_on=None,
    ):
        self.marked = list(_ALL_FIELDS if marked is None else marked)
        self.after_next = list(self.marked + ["publish"] if after_next is None else after_next)
        self.readback = readback
        self.chosen = chosen
        self.boost_on = boost_on
        self.listing_id = listing_id
        self.next_enabled = next_enabled
        self.fail_on = fail_on or {}
        self.actions: list = []
        self._pressed_next = False

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
        self.actions.append(("navigate", url))

    def navigate(self, url):
        self.navigate_visible(url)

    def call_tool(self, name, arguments):
        target = arguments.get("target", "")
        step = target.split("'")[1] if "'" in target else name
        self.actions.append((name, step))
        if name in self.fail_on or step in self.fail_on:
            raise BrowserToolError(self.fail_on.get(name) or self.fail_on.get(step))
        if step == "next":
            self._pressed_next = True
        return ""

    def evaluate(self, function, **kwargs):
        if function == _ADAPTER.publish_fields_js:
            marked = self.after_next if self._pressed_next else self.marked
            return {
                "marked": list(marked),
                "missing": [],
                "next_enabled": self.next_enabled,
                "publish_enabled": True,
                "boost_on": self.boost_on,
            }
        if function == _ADAPTER.publish_readback_js:
            return self.readback if self.readback is not None else dict(_GOOD_READBACK)
        if function == _ADAPTER.publish_result_js:
            return {"listing_id": self.listing_id, "url": _listing_url(self.listing_id)}
        # An options artifact, built per call with the wanted text baked in.
        return {"chosen": self.chosen, "options": ["New", "Used - Good", "Miscellaneous"]}


def _listing_url(listing_id):
    return f"https://www.facebook.com/marketplace/item/{listing_id}/" if listing_id else ""


_ITEM = {
    "id": "item_1",
    "title": "White Study Desk",
    "list_price": 65.0,
    "currency": "SGD",
    "description": "Light scuffs on the desktop.",
    "condition": "Used - Good",
}
_GOOD_READBACK = {
    "title": "White Study Desk",
    "price": "$65",
    "description": "Light scuffs on the desktop.",
    "condition": "Used - Good",
    "category": "Miscellaneous",
}


def _publish(client, item=None, **kwargs):
    return publisher.publish(
        client, _ADAPTER, item or _ITEM, create_url=_CREATE, sleep=lambda _s: None, **kwargs
    )


def _steps(client):
    return [step for name, step in client.actions if name == "browser_click"]


# --- the happy path -------------------------------------------------------------------------------


def test_a_filled_form_publishes_and_names_the_listing() -> None:
    client = StubForm()

    outcome = _publish(client)

    assert outcome.verified
    assert outcome.listing_id == "999"
    assert outcome.url == _listing_url("999")
    assert _steps(client)[-2:] == ["next", "publish"], "Next then Publish, in that order"


def test_the_title_and_price_are_typed_not_set() -> None:
    """A value assigned from script leaves React holding the old one, and publishes an empty
    listing. Every field goes in as real input."""
    client = StubForm()

    _publish(client)

    typed = {step: name for name, step in client.actions if name == "browser_type"}
    assert set(typed) == {"title", "price", "description"}


def test_the_price_is_typed_without_separators() -> None:
    """ "1,299" has been read as 1 by more than one marketplace form."""
    client = StubForm(readback={**_GOOD_READBACK, "title": "Piano", "price": "$1299"})

    _publish(client, item={**_ITEM, "title": "Piano", "list_price": 1299.0})

    price = [a for a in client.actions if a[0] == "browser_type" and a[1] == "price"]
    assert price, "the price was never typed"


# --- refusals before anything exists --------------------------------------------------------------


def test_a_form_missing_its_fields_is_never_filled_in() -> None:
    """The two text inputs are indistinguishable except by the label beside them and they sit one
    above the other, so a form we only half recognise could put the price in the title."""
    client = StubForm(marked=["price", "next"])

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert not [a for a in client.actions if a[0] == "browser_type"]


def test_a_dropdown_with_no_matching_option_stops_before_the_commit() -> None:
    """Facebook requires both dropdowns, so carrying on would press Publish against a form that
    refuses — and that failure arrives with the listing half made and no way to tell."""
    client = StubForm(chosen=None)

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert "next" not in _steps(client)


@pytest.mark.parametrize("field,seen", [("title", "White Study Des"), ("price", "$6")])
def test_a_form_that_did_not_take_what_we_gave_it_never_publishes(field, seen) -> None:
    """The last moment a mistake is free. A field that silently truncated becomes a live listing
    the seller has to find and fix."""
    client = StubForm(readback={**_GOOD_READBACK, field: seen})

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert "next" not in _steps(client)


def test_a_paid_boost_that_is_on_is_turned_off() -> None:
    """It spends the seller's money on something they never asked for."""
    client = StubForm(boost_on=True)
    # The switch answers off once it has been clicked.
    real_evaluate = client.evaluate

    def evaluate(function, **kwargs):
        answer = real_evaluate(function, **kwargs)
        if function == _ADAPTER.publish_fields_js and "boost" in _steps(client):
            return {**answer, "boost_on": False}
        return answer

    client.evaluate = evaluate

    _publish(client)

    assert "boost" in _steps(client)


def test_a_paid_boost_that_will_not_turn_off_refuses_to_publish() -> None:
    """Refusing to list is the cheaper failure: the seller can ask again, where a boost they never
    asked for spends real money and cannot be taken back."""
    client = StubForm(boost_on=True)

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert "next" not in _steps(client)


def test_photographs_that_will_not_attach_stop_the_publish() -> None:
    client = StubForm(fail_on={"browser_file_upload": "the file input went away"})

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client, photos=["/tmp/a.jpg"])

    assert "next" not in _steps(client)


def test_a_market_with_no_publish_selectors_is_refused_outright() -> None:
    import dataclasses

    stripped = dataclasses.replace(_ADAPTER, publish_fields_js="")

    with pytest.raises(publisher.PublishNotAttempted):
        publisher.publish(StubForm(), stripped, _ITEM, create_url=_CREATE, sleep=lambda _s: None)


# --- past the point of no return ------------------------------------------------------------------


def test_a_failure_after_next_is_unverified_and_never_retried() -> None:
    """A Next that lands and a Publish that does not still leaves a draft, and re-driving it would
    give the seller two listings. The exception type is the whole decision."""
    client = StubForm(fail_on={"publish": "the button went away"})

    with pytest.raises(publisher.PublishUnverified):
        _publish(client)


def test_a_form_that_moves_on_without_offering_publish_is_unverified() -> None:
    client = StubForm(after_next=["title", "price"])

    with pytest.raises(publisher.PublishUnverified):
        _publish(client)


def test_a_publish_whose_listing_cannot_be_named_is_reported_unverified() -> None:
    """Not an error — but not a success either. Reported for a human rather than retried into a
    duplicate, which is the same fail-closed rule the send bracket uses."""
    client = StubForm(listing_id=None)

    outcome = _publish(client)

    assert outcome.verified is False
    assert outcome.listing_id is None
    assert "could not be identified" in outcome.reason


# --- the condition map ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said,expected",
    [
        ("Brand new", "New"),
        ("new", "New"),
        ("Like new", "Used - Like New"),
        ("Open box", "Used - Like New"),
        ("Lightly used", "Used - Good"),
        ("Used - Good", "Used - Good"),
        ("Fair", "Used - Fair"),
        ("Heavily used", "Used - Fair"),
        ("", "Used - Good"),
        ("something nobody has seen", "Used - Good"),
    ],
)
def test_a_condition_is_mapped_to_this_markets_own_words(said, expected) -> None:
    """Conditions are free text on an item — they come from whatever another marketplace called it.
    Where the two do not meet this understates: saying a thing is more used than it is costs the
    seller a little, and the reverse is a lie told on their behalf."""
    assert publisher._condition_for({"condition": said}) == expected


def test_every_mapped_condition_is_one_this_market_offers() -> None:
    """A condition the dropdown does not offer is a publish that fails at the last moment."""
    from sellee.browser.markets import facebook

    for said in ("Brand new", "Like new", "Lightly used", "Fair", "", "unknown"):
        assert publisher._condition_for({"condition": said}) in facebook.CONDITIONS


def test_the_default_category_is_one_this_market_offers() -> None:
    from sellee.browser.markets import facebook

    assert facebook.DEFAULT_CATEGORY == "Miscellaneous"
    assert _ADAPTER.publish_default_category == facebook.DEFAULT_CATEGORY


def test_a_form_that_is_not_ready_is_never_pressed_and_stays_retryable() -> None:
    """Facebook greys Next out until it has everything it requires — a photograph among them — and
    clicking a disabled button submits nothing. Read as "may have gone through", a missing photo
    would retire the item forever; it is exactly the case that should be tried again."""
    client = StubForm(next_enabled=False)

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert "next" not in _steps(client)


def test_the_photo_chooser_is_opened_before_the_files_are_handed_over() -> None:
    """The browser server only accepts an upload while a chooser is actually open, so handing it
    paths without pressing Add photos first fails on a form that is otherwise perfect."""
    client = StubForm()

    _publish(client, photos=["/tmp/a.jpg"])

    order = [
        step for name, step in client.actions if name in ("browser_click", "browser_file_upload")
    ]
    assert order.index("add_photos") < order.index("browser_file_upload")


# --- confirming a publish the landing page does not name --------------------------------------


class ConfirmingForm(StubForm):
    """A form whose publish lands somewhere that names no listing — Facebook's selling page — so
    confirmation has to come from the seller's own listings instead."""

    def __init__(self, *, listings=None, **kwargs):
        super().__init__(listing_id=None, **kwargs)
        self.listings = listings

    def evaluate(self, function, **kwargs):
        if function == _ADAPTER.my_listings_entry_js:
            return {"url": "/marketplace/profile/1/"}
        if function == _ADAPTER.my_listings_js:
            return {"listings": list(self.listings or []), "active_count": len(self.listings or [])}
        return super().evaluate(function, **kwargs)


def _row(listing_id, title):
    return {
        "listing_id": listing_id,
        "title": title,
        "url": f"https://www.facebook.com/marketplace/item/{listing_id}/",
        "price": 15.0,
        "price_text": "SGD15",
    }


def test_a_publish_is_confirmed_from_the_sellers_own_listings() -> None:
    """Facebook redirects to its selling page, whose cards carry no listing id at all — so the
    listing is found among the seller's own, which is the one view that has both."""
    client = ConfirmingForm(listings=[_row("777", "White Study Desk"), _row("888", "Something")])

    outcome = _publish(client, listings_url="https://www.facebook.com/marketplace/you/selling")

    assert outcome.verified
    assert outcome.listing_id == "777"


def test_two_listings_sharing_the_title_leave_the_publish_unverified() -> None:
    """The seller already had one. Claiming either id would record a URL pointing at the wrong
    listing, so buyers on the new one would never reach this item."""
    client = ConfirmingForm(
        listings=[_row("777", "White Study Desk"), _row("888", "White Study Desk")]
    )

    outcome = _publish(client, listings_url="https://www.facebook.com/marketplace/you/selling")

    assert outcome.verified is False
    assert outcome.listing_id is None


def test_confirmation_is_skipped_when_there_is_nowhere_to_look() -> None:
    client = ConfirmingForm(listings=[_row("777", "White Study Desk")])

    outcome = _publish(client)

    assert outcome.verified is False


# --- staging the item's photographs ---------------------------------------------------------


def test_photographs_are_staged_from_the_shape_an_item_stores_them_in(
    tmp_path, monkeypatch
) -> None:
    """An item stores each photograph as a mapping, not a path. Taking `str()` of it stringified
    the whole mapping into a filename hundreds of characters long, so every copy failed and every
    publish went out with no photograph — and Facebook keeps Next disabled until it has one, so the
    lane re-drove the same item every thirty seconds forever."""
    from sellee import paths

    monkeypatch.setattr(paths, "publish_staging_dir", lambda: tmp_path / "staging")
    real = tmp_path / "01.jpg"
    real.write_bytes(b"\xff\xd8\xff" + b"0" * 32)

    staged = publisher.stage_photos("item_1", [{"path": str(real), "uploaded_url": "x" * 900}])

    assert len(staged) == 1
    assert open(staged[0], "rb").read() == real.read_bytes()


def test_a_bare_path_still_stages(tmp_path, monkeypatch) -> None:
    from sellee import paths

    monkeypatch.setattr(paths, "publish_staging_dir", lambda: tmp_path / "staging")
    real = tmp_path / "01.jpg"
    real.write_bytes(b"\xff\xd8\xff")

    assert len(publisher.stage_photos("item_1", [str(real)])) == 1


def test_a_photograph_with_no_path_is_skipped_rather_than_crashing(tmp_path, monkeypatch) -> None:
    from sellee import paths

    monkeypatch.setattr(paths, "publish_staging_dir", lambda: tmp_path / "staging")

    assert publisher.stage_photos("item_1", [{"uploaded_url": "x"}, {}, ""]) == []
