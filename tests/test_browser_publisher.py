"""Publishing by driving the form: what it refuses to do, and what it will never do twice.

Before the commit a listing does not exist and anything wrong is free; after it, the only safe move
is to stop. What is held here is that line: which failures leave the work retryable, which retire
it, and that nothing is pressed until the form has been read back and agrees with what it was
given.
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
        if function == _ADAPTER.publish.fields_js:
            marked = self.after_next if self._pressed_next else self.marked
            return {
                "marked": list(marked),
                "missing": [],
                "next_enabled": self.next_enabled,
                "publish_enabled": True,
                "boost_on": self.boost_on,
            }
        if function == _ADAPTER.publish.readback_js:
            return self.readback if self.readback is not None else dict(_GOOD_READBACK)
        if function == _ADAPTER.publish.result_js:
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
    """A value assigned from script leaves React holding the old one, so every field goes in as
    real input."""
    client = StubForm()

    _publish(client)

    typed = {step: name for name, step in client.actions if name == "browser_type"}
    assert set(typed) == {"title", "price", "description"}


def test_the_price_is_typed_without_separators() -> None:
    """ "1,299" has been read as 1 by more than one marketplace form."""
    client = StubForm(readback={**_GOOD_READBACK, "title": "Piano", "price": "$1299"})

    _publish(client, item={**_ITEM, "title": "Piano", "list_price": 1299.0})

    assert client.typed.get("price") == "1299"


# --- refusals before anything exists --------------------------------------------------------------


def test_a_form_missing_its_fields_is_never_filled_in() -> None:
    """The two text inputs are distinguishable only by their labels and sit one above the other,
    so a half-recognised form could put the price in the title."""
    client = StubForm(marked=["price", "next"])

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert not [a for a in client.actions if a[0] == "browser_type"]


def test_a_dropdown_with_no_matching_option_stops_before_the_commit() -> None:
    """Facebook requires both dropdowns, so carrying on would press Publish against a form that
    refuses."""
    client = StubForm(chosen=None)

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert "next" not in _steps(client)


@pytest.mark.parametrize("field,seen", [("title", "White Study Des"), ("price", "$6")])
def test_a_form_that_did_not_take_what_we_gave_it_never_publishes(field, seen) -> None:
    """A field that silently truncated becomes a live listing the seller has to find and fix."""
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
        if function == _ADAPTER.publish.fields_js and "boost" in _steps(client):
            return {**answer, "boost_on": False}
        return answer

    client.evaluate = evaluate

    _publish(client)

    assert "boost" in _steps(client)


def test_a_paid_boost_that_will_not_turn_off_refuses_to_publish() -> None:
    """Refusing to list is the cheaper failure; a boost spends real money and cannot be taken
    back."""
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

    stripped = dataclasses.replace(_ADAPTER, publish=None)

    with pytest.raises(publisher.PublishNotAttempted):
        publisher.publish(StubForm(), stripped, _ITEM, create_url=_CREATE, sleep=lambda _s: None)


# --- past the point of no return ------------------------------------------------------------------


def test_a_failure_after_next_is_unverified_and_never_retried() -> None:
    """A Next that lands and a Publish that does not still leaves a draft; re-driving it would
    give the seller two listings."""
    client = StubForm(fail_on={"publish": "the button went away"})

    with pytest.raises(publisher.PublishUnverified):
        _publish(client)


def test_a_form_that_moves_on_without_offering_publish_is_unverified() -> None:
    client = StubForm(after_next=["title", "price"])

    with pytest.raises(publisher.PublishUnverified):
        _publish(client)


def test_a_publish_whose_listing_cannot_be_named_is_reported_unverified() -> None:
    """Not an error, but not retried into a duplicate either."""
    client = StubForm(listing_id=None)

    outcome = _publish(client)

    assert outcome.verified is False
    assert outcome.listing_id is None
    assert "could not be identified" in outcome.reason


def test_the_category_dropdown_is_asked_for_the_adapters_default() -> None:
    """The driver files under the adapter's default category — nothing here may choose one — so
    the category dropdown must actually be asked for that word."""
    client = StubForm()

    _publish(client)

    assert any(_ADAPTER.publish.default_category in query for query in client.option_queries), (
        "no options artifact carried the default category"
    )


def test_a_form_that_is_not_ready_is_never_pressed_and_stays_retryable() -> None:
    """A disabled Next submits nothing, and reading that as "may have gone through" would retire a
    retryable item."""
    client = StubForm(next_enabled=False)

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert "next" not in _steps(client)


def test_the_photo_chooser_is_opened_before_the_files_are_handed_over() -> None:
    """The browser server only accepts an upload while the chooser is actually open."""
    client = StubForm()

    _publish(client, photos=["/tmp/a.jpg"])

    order = [
        step for name, step in client.actions if name in ("browser_click", "browser_file_upload")
    ]
    assert order.index("add_photos") < order.index("browser_file_upload")


# --- confirming a publish the landing page does not name --------------------------------------


class ConfirmingForm(StubForm):
    """A form whose publish lands on a page that names no listing; confirmation comes from the
    seller's own listings."""

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
    """Facebook's selling page carries no listing ids, so the listing is found among the seller's
    own."""
    client = ConfirmingForm(listings=[_row("777", "White Study Desk"), _row("888", "Something")])

    outcome = _publish(client, listings_url="https://www.facebook.com/marketplace/you/selling")

    assert outcome.verified
    assert outcome.listing_id == "777"


def test_two_listings_sharing_the_title_leave_the_publish_unverified() -> None:
    """Claiming either id would record a URL pointing at the wrong listing."""
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
    """An item stores each photograph as a mapping, not a path; staging must take the path from
    inside it."""
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


def test_the_settles_between_form_steps_are_jittered(monkeypatch) -> None:
    """The point is variance, not slowness: the average pause is unchanged."""
    import statistics

    slept: list = []
    monkeypatch.setattr("time.sleep", slept.append)

    for _ in range(400):
        publisher._sleep(publisher.STEP_SETTLE_SEC)

    assert len(set(slept)) > 300, "the pause is effectively constant"
    assert min(slept) >= publisher.STEP_SETTLE_SEC * (1 - publisher._JITTER)
    assert max(slept) <= publisher.STEP_SETTLE_SEC * (1 + publisher._JITTER)
    # Jitter, not delay: the mean is where it always was.
    assert statistics.mean(slept) == pytest.approx(publisher.STEP_SETTLE_SEC, rel=0.08)
