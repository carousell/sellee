"""Facebook's publish choreography: the order its create form insists on, and what it refuses.

The form is scripted here, because the only way to learn what Facebook's really does is to drive
it. What these hold is Facebook's own sequence — expand, mark, attach, type, choose, then Next
before Publish — and the two gates it answers. The line between "nothing was created" and "a
listing may exist" is the flow's, held in `test_browser_publisher.py`.
"""

from __future__ import annotations

import pytest

from sellee.browser import markets as market_adapters
from sellee.browser import publisher
from sellee.browser.client import BrowserToolError
from sellee.browser.markets import facebook
from sellee.browser.markets.publishing import PublishNotAttempted, PublishUnverified

_SURFACE = facebook.PUBLISH_SURFACE
_CREATE = "https://www.facebook.com/marketplace/create/item"

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
    """A create form answering Facebook's publish artifacts from a script, recording what was
    done to it."""

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
        # What was actually typed into each field, so a test can hold the text and not merely
        # that a call happened.
        self.typed: dict = {}
        # Every options artifact evaluated, with the wanted text baked in.
        self.option_queries: list = []
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
        if name == "browser_type":
            self.typed[step] = arguments.get("text")
        if name in self.fail_on or step in self.fail_on:
            raise BrowserToolError(self.fail_on.get(name) or self.fail_on.get(step))
        if step == "next":
            self._pressed_next = True
        return ""

    def evaluate(self, function, **kwargs):
        if function == facebook.PUBLISH_FIELDS_JS:
            marked = self.after_next if self._pressed_next else self.marked
            return {
                "marked": list(marked),
                "missing": [],
                "next_enabled": self.next_enabled,
                "publish_enabled": True,
                "boost_on": self.boost_on,
            }
        if function == facebook.PUBLISH_READBACK_JS:
            return self.readback if self.readback is not None else dict(_GOOD_READBACK)
        if function == facebook.PUBLISH_RESULT_JS:
            return {"listing_id": self.listing_id, "url": _listing_url(self.listing_id)}
        # An options artifact, built per call with the wanted text baked in.
        self.option_queries.append(function)
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


def _never_pauses(_seconds):
    return None


def _prepare(client, item=None, photos=()):
    return _SURFACE.prepare(client, item or _ITEM, photos, _never_pauses)


def _steps(client):
    return [step for name, step in client.actions if name == "browser_click"]


# --- filling the form -----------------------------------------------------------------------------


def test_the_form_is_filled_in_the_order_facebook_wants_it() -> None:
    """ "More details" first, because the description is behind it; the photo chooser before the
    files; the dropdowns after the text, because opening one covers the fields."""
    client = StubForm()

    _prepare(client, photos=["/tmp/a.jpg"])

    assert [
        step
        for name, step in client.actions
        if name in ("browser_click", "browser_type", "browser_file_upload")
    ] == [
        "more",
        "add_photos",
        "browser_file_upload",
        "title",
        "price",
        "description",
        "condition",
        "option",
        "category",
        "option",
    ]


def test_the_text_fields_are_typed_not_assigned() -> None:
    """A value set from script leaves React holding the old one, so every field goes in as real
    input."""
    client = StubForm()

    _prepare(client)

    assert set(step for name, step in client.actions if name == "browser_type") == {
        "title",
        "price",
        "description",
    }


def test_the_price_is_typed_without_separators() -> None:
    """ "1,299" has been read as 1 by more than one marketplace form."""
    client = StubForm()

    _prepare(client, item={**_ITEM, "list_price": 1299.0})

    assert client.typed.get("price") == "1299"


def test_the_category_dropdown_is_asked_for_the_adapters_default() -> None:
    """Choosing the right category from a title is the listing flow's judgement, not a driver's,
    so the driver files under the one word the adapter names."""
    client = StubForm()

    _prepare(client)

    assert any(_SURFACE.default_category in query for query in client.option_queries), (
        "no options artifact carried the default category"
    )


def test_the_condition_dropdown_is_asked_for_facebooks_own_word() -> None:
    """An item's condition is free text; the dropdown offers four."""
    client = StubForm()

    _prepare(client, item={**_ITEM, "condition": "opened but like new"})

    assert any("Used - Like New" in query for query in client.option_queries)


def test_a_form_missing_its_fields_is_never_filled_in() -> None:
    """The two text inputs are distinguishable only by their labels and sit one above the other,
    so a half-recognised form could put the price in the title."""
    client = StubForm(marked=["price", "next"])

    with pytest.raises(PublishNotAttempted):
        _prepare(client)

    assert not [a for a in client.actions if a[0] == "browser_type"]


def test_a_dropdown_with_no_matching_option_refuses() -> None:
    """Facebook requires both dropdowns, so carrying on would press Publish against a form that
    refuses."""
    client = StubForm(chosen=None)

    with pytest.raises(PublishNotAttempted):
        _prepare(client)


def test_photographs_that_will_not_attach_stop_the_publish() -> None:
    client = StubForm(fail_on={"browser_file_upload": "the file input went away"})

    with pytest.raises(PublishNotAttempted):
        _prepare(client, photos=["/tmp/a.jpg"])


def test_the_photo_chooser_is_opened_before_the_files_are_handed_over() -> None:
    """The browser server only accepts an upload while the chooser is actually open."""
    client = StubForm()

    _prepare(client, photos=["/tmp/a.jpg"])

    order = [
        step for name, step in client.actions if name in ("browser_click", "browser_file_upload")
    ]
    assert order.index("add_photos") < order.index("browser_file_upload")


def test_a_form_that_is_not_ready_is_refused_while_that_is_still_free() -> None:
    """Facebook greys Next out until it has everything it requires, and clicking a disabled button
    submits nothing — reading that as "may have gone through" would retire a retryable item."""
    client = StubForm(next_enabled=False)

    with pytest.raises(PublishNotAttempted):
        _prepare(client)

    assert "next" not in _steps(client)


def test_a_form_that_will_not_expand_is_filled_anyway() -> None:
    """ "More details" is best-effort: the marking pass that follows decides whether the form is
    usable."""
    client = StubForm(fail_on={"more": "the expander went away"})

    _prepare(client)

    assert client.typed.get("title") == "White Study Desk"


# --- the gates ------------------------------------------------------------------------------------


@pytest.mark.parametrize("field,seen", [("title", "White Study Des"), ("price", "$6")])
def test_a_form_that_did_not_take_what_we_gave_it_never_publishes(field, seen) -> None:
    """A field that silently truncated becomes a live listing the seller has to find and fix."""
    client = StubForm(readback={**_GOOD_READBACK, field: seen})

    with pytest.raises(PublishNotAttempted):
        _SURFACE.verify_form(client, _ITEM)


def test_a_paid_boost_that_is_on_is_turned_off() -> None:
    """It spends the seller's money on something they never asked for."""
    client = StubForm(boost_on=True)
    # The switch answers off once it has been clicked.
    real_evaluate = client.evaluate

    def evaluate(function, **kwargs):
        answer = real_evaluate(function, **kwargs)
        if function == facebook.PUBLISH_FIELDS_JS and "boost" in _steps(client):
            return {**answer, "boost_on": False}
        return answer

    client.evaluate = evaluate

    _SURFACE.refuse_paid_extras(client)

    assert "boost" in _steps(client)


def test_a_paid_boost_that_will_not_turn_off_refuses_to_publish() -> None:
    """Refusing to list is the cheaper failure; a boost spends real money and cannot be taken
    back."""
    client = StubForm(boost_on=True)

    with pytest.raises(PublishNotAttempted):
        _SURFACE.refuse_paid_extras(client)


def test_a_form_with_no_boost_switch_showing_is_left_alone() -> None:
    client = StubForm()

    _SURFACE.refuse_paid_extras(client)

    assert "boost" not in _steps(client)


# --- past the point of no return ------------------------------------------------------------------


def test_the_commit_is_next_then_publish() -> None:
    client = StubForm()

    _SURFACE.commit(client, _never_pauses)

    assert _steps(client) == ["next", "publish"], "Next then Publish, in that order"


def test_a_publish_button_that_never_appears_is_unverified() -> None:
    """The Next has already been pressed, so a draft may exist; re-driving it would give the seller
    two listings."""
    client = StubForm(after_next=["title", "price"])

    with pytest.raises(PublishUnverified):
        _SURFACE.commit(client, _never_pauses)


# --- the whole thing, through the flow ------------------------------------------------------------


def test_facebook_publishes_end_to_end_on_the_real_adapter() -> None:
    """Holds the wiring the pieces above cannot: that the adapter carries this surface, and that
    the flow's gates and bracket accept the choreography as written."""
    client = StubForm()

    outcome = publisher.publish(
        client,
        market_adapters.FACEBOOK,
        _ITEM,
        create_url=_CREATE,
        photos=["/tmp/a.jpg"],
        sleep=_never_pauses,
    )

    assert outcome.verified
    assert outcome.listing_id == "999"
    assert outcome.url == _listing_url("999")
    assert _steps(client)[-2:] == ["next", "publish"]
