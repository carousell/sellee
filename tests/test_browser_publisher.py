"""Publishing by driving the form: what it refuses to do, and what it will never do twice.

Before the commit a listing does not exist and anything wrong is free; after it, the only safe move
is to stop. What is held here is that line: which failures leave the work retryable, which retire
it, and that nothing is pressed until the form has been read back and agrees with what it was
given.
"""

from __future__ import annotations

import random
import threading

import pytest

from sellee.browser import client as client_mod
from sellee.browser import formfill, publisher
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserClient, BrowserToolError

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


class _NoJitter:
    def uniform(self, low: float, high: float) -> float:
        return 0.0


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
        wall="",
        misses=0,
        place="San Francisco, CA, US 94103",
    ):
        # The real typing, so what a publish test says the form received is what it would have.
        # What the wall probe answers: '' is a marketplace that is not refusing the account.
        self.wall = wall
        self._lock = threading.RLock()
        self._sleep = lambda _seconds: None
        self._rng = random.Random(0)
        # Which field holds the caret: a pressed key carries no target and lands there.
        self.focused = None
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
        self.dropped = None
        # What each dropdown and the location now hold; `misses` presses on an option that the
        # form does not take, as a long menu's own scroll can make one miss.
        self.picked: dict = {}
        self.misses = misses
        self.place = place
        self._open = None
        self._offered = None
        self._highlighted = None

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

    type_humanly = BrowserClient.type_humanly
    _click_into = BrowserClient._click_into

    def click(self, target, element):
        """The page receives a click on the control; how the cursor got there is the client's."""
        return self.call_tool("browser_click", {"target": target, "element": element})

    def call_tool(self, name, arguments):
        target = arguments.get("target", "")
        step = target.split("'")[1] if "'" in target else name
        self.actions.append((name, step))
        if name == "browser_click":
            self.focused = step
            if step in ("condition", "category"):
                self._open = step
            elif step == "option" and self._open:
                if self.misses:
                    self.misses -= 1
                else:
                    self.picked[self._open] = self._offered
        if name == "browser_press_key" and arguments.get("key") == "ArrowDown":
            self._highlighted = self.focused == "location" and self.place
        if name == "browser_press_key" and arguments.get("key") == "Enter" and self._highlighted:
            self.picked["location"] = self._highlighted
        if name == "browser_press_key" and self.focused:
            key = arguments.get("key", "")
            if len(key) == 1 or key == "Shift+Enter":
                char = "\n" if key == "Shift+Enter" else key
                self.typed[self.focused] = self.typed.get(self.focused, "") + char
        if name == "browser_type":
            self.typed[step] = self.typed.get(step, "") + arguments.get("text")
        if name in self.fail_on or step in self.fail_on:
            raise BrowserToolError(self.fail_on.get(name) or self.fail_on.get(step))
        if name == "browser_file_upload":
            # As in Chrome: pressing "Add photos" opens no chooser to hand files to.
            raise BrowserToolError("can only be used when there is related modal state present")
        if name == "browser_drop":
            self.dropped = (step, list(arguments.get("paths") or []))
        if step == "next":
            self._pressed_next = True
        return ""

    def evaluate(self, function, **kwargs):
        if function == client_mod.FOCUS_BOX_JS:
            target = kwargs.get("target") or ""
            self.focused = target.split("'")[1] if "'" in target else target
            return True
        if function == client_mod.HAS_CARET_JS:
            return True
        if function == _ADAPTER.block_wall_js:
            return self.wall
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
            held = dict(self.readback if self.readback is not None else _GOOD_READBACK)
            return {**held, **self.picked}
        if function == _ADAPTER.publish_result_js:
            return {"listing_id": self.listing_id, "url": _listing_url(self.listing_id)}
        if "const zip" in function:
            if self.place == "94103":
                # As live: the choice left the box holding the ZIP alone.
                self.picked["location"] = "94103"
                return {"chosen": "San Francisco, CA, US 94103", "at": 0}
            return {"chosen": self.place, "at": 0}
        # An options artifact, built per call with the wanted text baked in.
        self.option_queries.append(function)
        self._offered = self.chosen
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

    assert set(client.typed) == {"title", "price", "description"}


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

    assert not client.typed


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
        if function == _ADAPTER.publish_fields_js and "boost" in _steps(client):
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
    client = StubForm(fail_on={"browser_drop": "the drop area went away"})

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

    assert any(_ADAPTER.publish_default_category in query for query in client.option_queries), (
        "no options artifact carried the default category"
    )


def test_a_form_that_is_not_ready_is_never_pressed_and_stays_retryable() -> None:
    """A disabled Next submits nothing, and reading that as "may have gone through" would retire a
    retryable item."""
    client = StubForm(next_enabled=False)

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert "next" not in _steps(client)


def test_the_attempt_is_ledgered_before_next_is_pressed_and_not_before() -> None:
    client = StubForm()
    steps_at_ledger: list = []

    _publish(client, before_commit=lambda: steps_at_ledger.append(list(_steps(client))))

    assert len(steps_at_ledger) == 1 and "next" not in steps_at_ledger[0]
    assert "next" in _steps(client)


def test_a_form_refused_before_its_commit_ledgers_no_attempt() -> None:
    ledgered: list = []

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(StubForm(next_enabled=False), before_commit=lambda: ledgered.append(True))

    assert ledgered == []


def test_the_photographs_are_dropped_on_add_photos_before_next() -> None:
    """In Chrome, pressing "Add photos" opens no chooser, so the photographs are dropped on it."""
    client = StubForm()

    _publish(client, photos=["/tmp/a.jpg", "/tmp/b.jpg"])

    assert client.dropped == ("add_photos", ["/tmp/a.jpg", "/tmp/b.jpg"])
    assert "browser_file_upload" not in [name for name, _step in client.actions]
    assert client.actions.index(("browser_drop", "add_photos")) < client.actions.index(
        ("browser_click", "next")
    )


def test_a_form_with_nowhere_to_drop_photographs_is_not_attempted() -> None:
    client = StubForm(marked=[step for step in _ALL_FIELDS if step != "add_photos"])

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client, photos=["/tmp/a.jpg"])

    assert "next" not in _steps(client)


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
        formfill.sleep(formfill.STEP_SETTLE_SEC)

    assert len(set(slept)) > 300, "the pause is effectively constant"
    assert min(slept) >= formfill.STEP_SETTLE_SEC * (1 - formfill.JITTER)
    assert max(slept) <= formfill.STEP_SETTLE_SEC * (1 + formfill.JITTER)
    # Jitter, not delay: the mean is where it always was.
    assert statistics.mean(slept) == pytest.approx(formfill.STEP_SETTLE_SEC, rel=0.08)


def test_a_wall_stops_a_publish_before_anything_is_filled_in() -> None:
    """A wall is exactly the condition that clears, so nothing is created and the pair keeps its
    attempt."""
    client = StubForm(wall="automation")

    with pytest.raises(publisher.PublishNotAttempted) as caught:
        _publish(client)

    assert caught.value.retryable is True
    assert not client.typed


def test_a_wall_that_goes_up_mid_form_stops_before_the_commit() -> None:
    """A publish takes minutes and a wall can go up inside one. Everything past Next may have
    created a listing, so this is the last moment refusing still costs nothing."""
    client = StubForm()
    walls = {"n": 0}
    real_evaluate = client.evaluate

    def evaluate(function, **kwargs):
        if function == _ADAPTER.block_wall_js:
            walls["n"] += 1
            return "automation" if walls["n"] > 1 else ""
        return real_evaluate(function, **kwargs)

    client.evaluate = evaluate

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client)

    assert "next" not in _steps(client)
    assert "publish" not in _steps(client)


# --- what a failure before the commit says --------------------------------------------------------


def test_a_create_form_that_will_not_open_is_nothing_attempted() -> None:
    """Nothing exists yet, so it is safe to try again — never the bare error the fan-out would
    have had to treat as "a listing may exist"."""

    class WillNotOpen(StubForm):
        def navigate_visible(self, url):
            raise BrowserToolError("net::ERR_TIMED_OUT")

    with pytest.raises(publisher.PublishNotAttempted) as caught:
        _publish(WillNotOpen())

    assert caught.value.retryable is True


def test_running_out_of_page_loads_is_passed_through_untouched() -> None:
    """Being paced is not an attempt at all, and the fan-out spends nothing on it — so it is not
    dressed up as one."""
    from sellee.browser.governor import PagesSpent

    class Paced(StubForm):
        def navigate_visible(self, url):
            raise PagesSpent("spent")

    with pytest.raises(PagesSpent):
        _publish(Paced())


def test_a_next_button_that_moved_before_it_was_pressed_submitted_nothing() -> None:
    """The click refuses to press a control that moved, which is proof nothing was submitted —
    not the "may have gone through" every other failure past this point has to be."""
    from sellee.browser.client import ControlMoved

    class NextMoves(StubForm):
        def click(self, target, element):
            if element == "Next":
                raise ControlMoved("Next moved before it could be clicked")
            return super().click(target, element)

    with pytest.raises(publisher.PublishNotAttempted) as caught:
        _publish(NextMoves())

    assert caught.value.retryable is True


def test_a_publish_button_that_moved_is_still_treated_as_maybe_published() -> None:
    """Past Next the form may already have made the listing, so the conservative answer stands."""
    from sellee.browser.client import ControlMoved

    class PublishMoves(StubForm):
        def click(self, target, element):
            if element == "Publish":
                raise ControlMoved("Publish moved before it could be clicked")
            return super().click(target, element)

    with pytest.raises(publisher.PublishUnverified):
        _publish(PublishMoves())


# --- dropdowns and the location ------------------------------------------------------------------


def test_a_pick_the_form_did_not_take_is_pressed_again_and_publishes() -> None:
    client = StubForm(misses=1)

    _publish(client)

    assert client.picked["category"] == "ok"
    assert ("browser_click", "next") in client.actions


def test_a_pick_the_form_never_takes_stops_before_next() -> None:
    """Live, Category stayed empty after the press and Facebook greyed Next out."""
    client = StubForm(misses=9)

    with pytest.raises(publisher.PublishNotAttempted) as raised:
        _publish(client)

    assert raised.value.retryable
    assert "next" not in _steps(client)


def test_an_empty_location_is_given_the_place_facebook_suggests_for_the_zip() -> None:
    client = StubForm(marked=[*_ALL_FIELDS, "location"])

    _publish(client, seller={"zip": "94103"})

    assert client.typed.get("location") == "94103"
    assert client.picked["location"] == "San Francisco, CA, US 94103"
    enter = client.actions.index(("browser_press_key", "browser_press_key"))
    assert enter < client.actions.index(("browser_click", "next"))


def test_a_typed_zip_left_unchosen_stops_before_next() -> None:
    """Live, a press on the suggestion left the box holding the ZIP alone, which Facebook calls
    invalid; that is not a place."""
    client = StubForm(marked=[*_ALL_FIELDS, "location"], place="94103")

    with pytest.raises(publisher.PublishNotAttempted, match="did not take ZIP"):
        _publish(client, seller={"zip": "94103"})

    assert "next" not in _steps(client)


def test_a_location_the_form_already_holds_is_left_alone() -> None:
    client = StubForm(
        marked=[*_ALL_FIELDS, "location"], readback={**_GOOD_READBACK, "location": "Oakland, CA"}
    )

    _publish(client, seller={"zip": "94103"})

    assert "location" not in client.typed


def test_an_empty_location_with_no_zip_stops_before_next() -> None:
    client = StubForm(marked=[*_ALL_FIELDS, "location"])

    with pytest.raises(publisher.PublishNotAttempted, match="no ZIP"):
        _publish(client, seller={})

    assert "next" not in _steps(client)


def test_no_suggestion_for_the_zip_stops_before_next() -> None:
    client = StubForm(marked=[*_ALL_FIELDS, "location"], place=None)

    with pytest.raises(publisher.PublishNotAttempted, match="suggested no place"):
        _publish(client, seller={"zip": "94103"})

    assert "next" not in _steps(client)
